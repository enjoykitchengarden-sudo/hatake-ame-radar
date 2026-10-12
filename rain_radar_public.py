# -*- coding: utf-8 -*-
#
# 雨レーダー通知（1地点版）
# 気象庁の降水ナウキャストを見て、指定地点に雨が近づいたら ntfy で通知します。
#
# 使い方:
#   1. pip install pillow
#   2. 下の「設定」の LAT / LON / LOCATION_NAME / NTFY_TOPIC を書き換える
#   3. python rain_radar_public.py
#
# 注意: 気象庁のデータは日本周辺のみです。取得間隔を極端に短くしないでください。
# 注意： 雨の接近を保証するものではありません

import urllib.request
import urllib.parse
import json
import io
import math
import time
from datetime import datetime, timedelta
from PIL import Image


# =========================
# 設定（ここを書き換える）
# =========================

# 監視する地点（緯度・経度）。Googleマップで地点を右クリックすると確認できます
LAT = 35.0000   # ← 緯度
LON = 135.0000  # ← 経度

# 通知に表示する地点名
LOCATION_NAME = "監視地点"

# ntfy のトピック名。ntfy は名前を知っていれば誰でも読み書きできるため、
# 推測されにくい長い文字列にしてください
NTFY_TOPIC = "YOUR_NTFY_TOPIC"
NTFY_TITLE = "Rain Alert"
NTFY_PRIORITY = "high"

# 何秒おきにチェックするか
CHECK_INTERVAL = 5 * 60

# エラー時（データ更新途中など）の再試行間隔（秒）
RETRY_INTERVAL = 60

# 一度通知したあと、この時間は再通知しない（秒）
# 雨が降っている間に何度も通知が来るのを防ぎます。0 にすると無効
NOTIFY_COOLDOWN = 60 * 60


# =========================
# 距離（m）
# =========================

RADIUS_500M = 500
RADIUS_3KM = 3000
RADIUS_6KM = 6000

ZOOM = 10


# =========================
# 雨と判定する色
# =========================
# 白色（1mm/h 未満）は含めていません

RAIN_COLORS = [
    (160, 210, 255),  # 1～5 mm/h
    (33, 140, 255),   # 5～10 mm/h
    (0, 65, 255),     # 10～20 mm/h
    (250, 245, 0),    # 20～30 mm/h
    (255, 153, 0),    # 30～50 mm/h
    (254, 40, 3),     # 50～80 mm/h
    (181, 1, 103),    # 80 mm/h以上
]

# 雨色が何ピクセル以上あれば雨とするか（1ピクセルは約130m四方）
RAIN_PIXEL_THRESHOLD = 1


# =========================
# JSON取得
# =========================

def get_json(url):

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=20) as response:

        return json.loads(response.read().decode("utf-8"))


# =========================
# 時刻処理
# =========================

def parse_time(text):

    return datetime.strptime(text, "%Y%m%d%H%M%S")


def format_time(dt):

    return dt.strftime("%Y%m%d%H%M%S")


def get_current_time():

    data = get_json(
        "https://www.jma.go.jp/bosai/jmatile/data/nowc/"
        "targetTimes_N1.json"
    )

    if not data:

        raise Exception("N1のデータが空です")

    # 先頭が最新
    item = data[0]

    return item["basetime"], item["validtime"]


def get_forecast_time(current_validtime, minutes_after):

    data = get_json(
        "https://www.jma.go.jp/bosai/jmatile/data/nowc/"
        "targetTimes_N2.json"
    )

    if not data:

        raise Exception("N2のデータが空です")

    target_dt = (
        parse_time(current_validtime)
        + timedelta(minutes=minutes_after)
    )

    target_str = format_time(target_dt)

    for item in data:

        if item["validtime"] == target_str:

            return item["basetime"], item["validtime"]

    raise Exception(
        "{}分後のデータが見つかりません: {}".format(
            minutes_after,
            target_str
        )
    )


# =========================
# 緯度経度 → ピクセル座標
# =========================

def latlon_to_pixel(lat, lon):
    # 地図全体を通した共通ピクセル座標と、
    # その緯度での 1ピクセルあたりの距離(m) を返す

    n = 2 ** ZOOM

    x = (lon + 180.0) / 360.0 * n
    y = (
        1.0
        - math.asinh(math.tan(math.radians(lat))) / math.pi
    ) / 2.0 * n

    center_x = int(x * 256)
    center_y = int(y * 256)

    meters_per_pixel = (
        156543.03392 * math.cos(math.radians(lat)) / n
    )

    return center_x, center_y, meters_per_pixel


# =========================
# タイル取得
# =========================

def get_tile(basetime, validtime, x, y):

    url = (
        "https://www.jma.go.jp/bosai/jmatile/data/nowc/"
        + basetime
        + "/none/"
        + validtime
        + "/surf/hrpns/"
        + str(ZOOM)
        + "/"
        + str(x)
        + "/"
        + str(y)
        + ".png"
    )

    # 最大3回取得を試す
    for attempt in range(3):

        try:

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"}
            )

            with urllib.request.urlopen(req, timeout=20) as response:

                image_data = response.read()

            return Image.open(
                io.BytesIO(image_data)
            ).convert("RGBA")

        except Exception as e:

            print(
                "タイル取得失敗 x={} y={} ({}/3): {}".format(
                    x, y, attempt + 1, e
                )
            )

            if attempt < 2:

                time.sleep(2)

    print(
        "タイル取得失敗：3回とも取得できませんでした "
        "x={} y={}".format(x, y)
    )

    return None


# =========================
# 距離別の雨判定
# =========================
# 戻り値: True=雨あり / False=雨なし / None=判定不能（タイル取得失敗）

def is_rain(pixel_count):

    return pixel_count >= RAIN_PIXEL_THRESHOLD


def check_rain_range(
    basetime,
    validtime,
    min_distance,
    max_distance,
    tile_cache
):

    # 緯度経度から、タイルをまたぐ共通座標と距離の換算を自動計算
    center_x, center_y, meters_per_pixel = latlon_to_pixel(LAT, LON)

    # 判定範囲をピクセルに換算
    radius = int(max_distance / meters_per_pixel) + 1

    # 判定円にかかるタイルだけ取得する（はみ出す場合は隣のタイルも取得）
    min_tile_x = (center_x - radius) // 256
    max_tile_x = (center_x + radius) // 256
    min_tile_y = (center_y - radius) // 256
    max_tile_y = (center_y + radius) // 256

    rain_pixels = 0

    for tile_y in range(min_tile_y, max_tile_y + 1):

        for tile_x in range(min_tile_x, max_tile_x + 1):

            cache_key = (basetime, validtime, tile_x, tile_y)

            # 同じ時刻・タイルは1回だけ取得して再利用する
            if cache_key not in tile_cache:

                tile_cache[cache_key] = get_tile(
                    basetime, validtime, tile_x, tile_y
                )

            image = tile_cache[cache_key]

            if image is None:

                # 「雨なし」ではなく「判定不能」として返す
                print(
                    "タイル取得失敗のため判定できません: "
                    "X={} Y={}".format(tile_x, tile_y)
                )

                return None

            width, height = image.size

            # このタイル内で、判定円の外接範囲にかかる部分だけ調べる
            left = max(0, center_x - radius - tile_x * 256)
            right = min(width, center_x + radius - tile_x * 256 + 1)
            top = max(0, center_y - radius - tile_y * 256)
            bottom = min(height, center_y + radius - tile_y * 256 + 1)

            if left >= right or top >= bottom:

                continue

            for py in range(top, bottom):

                for px in range(left, right):

                    r, g, b, a = image.getpixel((px, py))

                    # 透明部分と、登録していない色（白色を含む）は無視
                    if a == 0 or (r, g, b) not in RAIN_COLORS:

                        continue

                    dx = tile_x * 256 + px - center_x
                    dy = tile_y * 256 + py - center_y

                    distance = (
                        (dx * dx + dy * dy) ** 0.5
                        * meters_per_pixel
                    )

                    if min_distance <= distance < max_distance:

                        rain_pixels += 1

    print(
        "距離 {}～{}m : {} pixels".format(
            min_distance, max_distance, rain_pixels
        )
    )

    return is_rain(rain_pixels)


# =========================
# 1回分の判定（3条件）
# =========================

def check_once():

    current_basetime, current_validtime = get_current_time()

    forecast5_basetime, forecast5_validtime = get_forecast_time(
        current_validtime, 5
    )

    forecast10_basetime, forecast10_validtime = get_forecast_time(
        current_validtime, 10
    )

    tile_cache = {}

    # 条件1：中心から500m以内、10分後
    condition1 = check_rain_range(
        forecast10_basetime, forecast10_validtime,
        0, RADIUS_500M, tile_cache
    )

    # 条件2：500m～3km、5分後
    condition2 = check_rain_range(
        forecast5_basetime, forecast5_validtime,
        RADIUS_500M, RADIUS_3KM, tile_cache
    )

    # 条件3：3～6km、現在
    condition3 = check_rain_range(
        current_basetime, current_validtime,
        RADIUS_3KM, RADIUS_6KM, tile_cache
    )

    return (
        condition1,
        condition2,
        condition3,
        current_validtime,
        forecast5_validtime,
        forecast10_validtime
    )


# =========================
# ntfy通知
# =========================

def send_ntfy(message):

    url = "https://ntfy.sh/" + urllib.parse.quote(NTFY_TOPIC)

    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": NTFY_TITLE,
            "Priority": NTFY_PRIORITY,
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": "RainRadar/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=20) as response:

        status = response.getcode()

    print("ntfy通知送信: HTTP {}".format(status))


# =========================
# 表示用（○ / × / 判定不能）
# =========================

def mark(condition):

    if condition is None:

        return "？"

    return "○" if condition else "×"


# =========================
# メイン監視
# =========================

def main():

    print("================================")
    print("      雨レーダー監視モード")
    print("================================")
    print("")
    print("{}秒おきにチェックします".format(CHECK_INTERVAL))
    print("Ctrl+C で終了")
    print("")

    # 現在、雨接近状態か
    rain_alert_active = False

    # 最後に通知した時刻（未通知は 0）
    last_notified = 0.0

    while True:

        try:

            print("")
            print("--------------------------------")
            print(
                "チェック開始: {}".format(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
            )
            print("--------------------------------")

            (
                condition1,
                condition2,
                condition3,
                current_time,
                forecast5_time,
                forecast10_time
            ) = check_once()

            print("")
            print("現在   : {}".format(current_time))
            print("5分後  : {}".format(forecast5_time))
            print("10分後 : {}".format(forecast10_time))
            print("")
            print("条件1 500m以内 10分後 : {}".format(mark(condition1)))
            print("条件2 500m～3km 5分後  : {}".format(mark(condition2)))
            print("条件3 3～6km 現在     : {}".format(mark(condition3)))

            conditions = (condition1, condition2, condition3)

            if False in conditions:

                # 1つでも確実に「雨なし」なら不成立
                all_conditions = False

            elif None in conditions:

                # 判定不能が残る場合は状態を変えずに今回はスキップ
                print("")
                print("判定不能の条件があるため、今回は状態を変更しません")
                print("")
                print("次回チェックまで{}秒...".format(CHECK_INTERVAL))

                time.sleep(CHECK_INTERVAL)

                continue

            else:

                all_conditions = True

            cooling_down = (
                time.time() - last_notified < NOTIFY_COOLDOWN
            )

            # ---------------------
            # × → ○ （クールダウン中）
            # ---------------------

            if (
                all_conditions
                and not rain_alert_active
                and cooling_down
            ):

                remaining = int(
                    (NOTIFY_COOLDOWN - (time.time() - last_notified)) / 60
                ) + 1

                print("")
                print(
                    "雨通知条件成立（通知済みから{}分以内のため通知しない・"
                    "あと約{}分）".format(
                        int(NOTIFY_COOLDOWN / 60),
                        remaining
                    )
                )

                # 同じ雨の続きとして扱う
                rain_alert_active = True

            # ---------------------
            # × → ○ （新しい雨接近）
            # ---------------------

            elif (
                all_conditions
                and not rain_alert_active
            ):

                print("")
                print("★★★ 雨通知条件成立 ★★★")
                print("→ 新しい雨接近イベント")

                message = (
                    "{}に雨が近づいています。\n"
                    "500m以内 10分後：雨あり\n"
                    "500m～3km 5分後：雨あり\n"
                    "3～6km 現在：雨あり"
                ).format(LOCATION_NAME)

                try:

                    send_ntfy(message)

                    # 送信成功したときだけ「通知済み」にする
                    rain_alert_active = True
                    last_notified = time.time()

                except Exception as e:

                    print("ntfy通知エラー（次回チェックで再送を試みます）:")
                    print(e)

            # ---------------------
            # ○ → ○
            # ---------------------

            elif all_conditions and rain_alert_active:

                print("")
                print("雨接近状態 継続中")
                print("→ 通知は出さない")

            # ---------------------
            # ○ → ×
            # ---------------------

            elif not all_conditions and rain_alert_active:

                print("")
                print("雨接近状態 終了")
                print("→ 次回また条件成立で通知可能")

                rain_alert_active = False

            # ---------------------
            # × → ×
            # ---------------------

            else:

                print("")
                print("雨通知条件なし")

            print("")
            print("次回チェックまで{}秒...".format(CHECK_INTERVAL))

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:

            print("")
            print("監視を終了しました")

            break

        except Exception as e:

            print("")
            print("エラー:")
            print(e)
            print("")
            print("{}秒後に再試行します...".format(RETRY_INTERVAL))

            time.sleep(RETRY_INTERVAL)


# =========================
# 実行
# =========================

if __name__ == "__main__":

    main()
