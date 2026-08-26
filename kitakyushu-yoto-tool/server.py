#!/usr/bin/env python3
"""北九州市 用途地域・建ぺい率・容積率 検索ツール — ローカルサーバー。

標準ライブラリのみで動作します（追加インストール不要）。

使い方:
    1. config.example.json を config.json にコピーし、
       reinfolib_api_key に取得したAPIキーを設定する
       (または環境変数 REINFOLIB_API_KEY を設定する)。
    2. `python3 server.py` を実行する。
    3. ブラウザで http://127.0.0.1:8765/ を開く。

住所の検索には国土地理院の住所検索APIを、用途地域・建ぺい率・容積率の
検索には国土交通省「不動産情報ライブラリ」API (XKT002) を利用しています。
APIキーはこのサーバープロセス内でのみ使用し、ブラウザには渡しません。
"""
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

GSI_GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
REINFOLIB_URL = "https://www.reinfolib.mlit.go.jp/ex-api/external/XKT002"
ZOOM = 15
REQUEST_TIMEOUT = 15


def load_api_key():
    key = os.environ.get("REINFOLIB_API_KEY")
    if key:
        return key
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("reinfolib_api_key")
        if key and "ここに" not in key:
            return key
    raise RuntimeError(
        "不動産情報ライブラリのAPIキーが設定されていません。"
        "config.json に reinfolib_api_key を設定するか、"
        "環境変数 REINFOLIB_API_KEY を設定してください。"
        "APIキーは https://www.reinfolib.mlit.go.jp/api/request/ から申請できます。"
    )


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8"))


def geocode(address):
    url = GSI_GEOCODE_URL + "?" + urllib.parse.urlencode({"q": address})
    results = fetch_json(url)
    if not results:
        return None
    top = results[0]
    lon, lat = top["geometry"]["coordinates"]
    title = top.get("properties", {}).get("title", address)
    return {"lat": lat, "lon": lon, "matched_title": title}


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
    )
    return x, y


def point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_intersect = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_intersect:
                inside = not inside
        j = i
    return inside


def point_in_geometry(lon, lat, geometry):
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        rings = coords
        if not rings or not point_in_ring(lon, lat, rings[0]):
            return False
        return not any(point_in_ring(lon, lat, hole) for hole in rings[1:])
    if gtype == "MultiPolygon":
        return any(
            point_in_geometry(lon, lat, {"type": "Polygon", "coordinates": poly})
            for poly in coords
        )
    return False


def find_zone(lon, lat, api_key):
    x, y = lonlat_to_tile(lon, lat, ZOOM)
    url = REINFOLIB_URL + "?" + urllib.parse.urlencode(
        {"response_format": "geojson", "z": ZOOM, "x": x, "y": y}
    )
    data = fetch_json(url, headers={"Ocp-Apim-Subscription-Key": api_key})
    for feature in data.get("features", []):
        geom = feature.get("geometry")
        if geom and point_in_geometry(lon, lat, geom):
            return feature.get("properties", {}), {"z": ZOOM, "x": x, "y": y}
    return None, {"z": ZOOM, "x": x, "y": y}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - silence default access log
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        path = os.path.join(BASE_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json(404, {"error": "file not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/lookup":
            self._handle_lookup(parsed)
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_file("frontend.html", "text/html; charset=utf-8")
            return
        self._send_json(404, {"error": "not found"})

    def _handle_lookup(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        address = (query.get("address") or [""])[0].strip()
        if not address:
            self._send_json(400, {"error": "address パラメータが必要です"})
            return

        try:
            api_key = load_api_key()
        except RuntimeError as e:
            self._send_json(500, {"error": str(e)})
            return

        try:
            geo = geocode(address)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            self._send_json(
                502, {"error": f"住所検索(国土地理院API)に失敗しました: {e}"}
            )
            return
        if geo is None:
            self._send_json(
                200,
                {"error": "住所が見つかりませんでした。表記を変えて再検索してください。"},
            )
            return

        in_scope = "北九州市" in geo["matched_title"] or "北九州市" in address

        try:
            props, tile = find_zone(geo["lon"], geo["lat"], api_key)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            self._send_json(
                502,
                {
                    "error": f"不動産情報ライブラリAPIの呼び出しに失敗しました (HTTP {e.code})。"
                    "APIキーが正しいか確認してください。",
                    "detail": detail,
                },
            )
            return
        except urllib.error.URLError as e:
            self._send_json(
                502, {"error": f"不動産情報ライブラリAPIへの接続に失敗しました: {e}"}
            )
            return

        result = {
            "input_address": address,
            "geocoded": geo,
            "in_scope": in_scope,
            "tile": tile,
            "zone": None,
            "error": None,
        }
        if props is None:
            result["error"] = (
                "この地点の用途地域データが見つかりませんでした。"
                "市街化調整区域など用途地域が指定されていない区域である可能性があります。"
                "北九州市都市計画課またはG-mottyでご確認ください。"
            )
        else:
            result["zone"] = {
                "use_area": props.get("use_area_ja"),
                "building_coverage_ratio": props.get("u_building_coverage_ratio_ja"),
                "floor_area_ratio": props.get("u_floor_area_ratio_ja"),
                "city_name": props.get("city_name"),
                "prefecture": props.get("prefecture"),
                "decision_date": props.get("decision_date"),
                "first_decision_date": props.get("first_decision_date"),
                "decision_classification": props.get("decision_classification"),
                "decision_maker": props.get("decision_maker"),
                "notice_number": props.get("notice_number"),
                "raw": props,
            }
        self._send_json(200, result)


def main():
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"起動しました: http://127.0.0.1:{port}/ (終了は Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
