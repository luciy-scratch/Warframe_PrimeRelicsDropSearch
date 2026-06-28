"""
main.py — Warframe Relic Searcher メインロジック
=========================================================
PyScript (Pyodide バックエンド) 上で動作し、以下を担当する:
  1. ドロップテーブルの非同期 Fetch (warframestat.us JSON API)
  2. JSON パース & レアリティ再分類
  3. Prime アイテム → パーツ → レリック のインデックス構築
  4. レリックマトリクス HTML の生成
     (横軸: レアリティ / 縦軸: パーツ名 / バッジ色: ティア)
  5. UI 状態管理 (Loading / Success / Error)
  6. 3 パネル独立のユーザーインタラクション

依存ライブラリ: Python 標準ライブラリのみ (外部パッケージ不要)
"""

import json
import re
import asyncio
from pyscript import document, when
from js import fetch as js_fetch  # ブラウザネイティブの fetch API を js モジュール経由で使用


# ============================================================
# 定数定義
# ============================================================

# 一次データソース: warframestat.us コミュニティ API (CORS 許可済み)
# all.slim.json より relics.json の方が軽量かつ構造が明確なため使用
DROP_TABLE_URL: str = "https://drops.warframestat.us/data/relics.json"

# フォールバック: API 取得失敗時に使用するローカルモックデータ
# TODO 開発用データが入っているので取得失敗を通知する内容に変えてもいいかも #
FALLBACK_URL: str = "./data_mock.json"

# Intact 時の確率によるレアリティ再分類テーブル
# ※ 公式ドロップテーブルの "Uncommon" 誤表記を補正するため、確率値で判定する
RARITY_BY_CHANCE: dict[float, str] = {
    25.33: "Common",    # 銅枠: 6 スロット中 3 枠
    11.00: "Uncommon",  # 銀枠: 6 スロット中 2 枠
    2.00:  "Rare",      # 金枠: 6 スロット中 1 枠
}

# テーブル横軸の表示順序 (抽選確率の高い順)
RARITY_ORDER: list[str] = ["Common", "Uncommon", "Rare"]

# レアリティ別ヘッダー文字色 (白背景に対して WCAG AA 以上のコントラスト比を確保)
RARITY_HEADER_CSS: dict[str, str] = {
    "Common":   "text-amber-700 text-shadow-sm text-shadow-orange-300",
    "Uncommon": "text-zinc-500 text-shadow-sm text-shadow-slate-400",
    "Rare":     "text-yellow-500 text-shadow-sm text-shadow-amber-200",
}

# Intact 時の確率ラベル (ヘッダーサブテキスト表示用)
RARITY_CHANCE_LABEL: dict[str, str] = {
    "Common":   "25.33%",
    "Uncommon": "11.00%",
    "Rare":     "2.00%",
}

# 処理対象のレリックティア (Requiem は除外)
TIER_ORDER: list[str] = ["Lith", "Meso", "Neo", "Axi"]

# ティア→表示順インデックス (レアリティ列内でのバッジ並び替えに使用)
TIER_SORT_KEY: dict[str, int] = {tier: idx for idx, tier in enumerate(TIER_ORDER)}

# ティア別バッジカラー (白背景向け: 暗色テキスト + 淡色背景でコントラスト強め)
TIER_CSS: dict[str, str] = {
    "Lith": "text-sky-700     border-sky-400     bg-sky-50",
    "Meso": "text-emerald-700 border-emerald-400 bg-emerald-50",
    "Neo":  "text-violet-700  border-violet-400  bg-violet-50",
    "Axi":  "text-amber-700   border-amber-400   bg-amber-50",
}

# Prime アイテム名とパーツ名を分離する正規表現
# 例: "Nova Prime Neuroptics Blueprint" → Group1="Nova Prime", Group2="Neuroptics Blueprint"
PRIME_PART_PATTERN: re.Pattern = re.compile(r"^(.+?\sPrime)\s+(.+)$")

# パーツ名の表示優先順位 (フレーム標準パーツを先頭に固定)
PART_PRIORITY: list[str] = [
    "Blueprint",
    "Neuroptics Blueprint",
    "Chassis Blueprint",
    "Systems Blueprint",
]

# 独立した検索・結果表示パネルの数
PANEL_COUNT: int = 3


# ============================================================
# アプリケーション状態 (グローバル変数)
# ============================================================

# prime_index: {Prime名: {パーツ名: [relic_info, ...]}}
prime_index: dict = {}

# ソート済みの全 Prime アイテム名リスト
all_prime_items: list[str] = []


# ============================================================
# データ取得
# ============================================================

async def fetch_drop_table(url: str) -> dict:
    """
    指定された URL からドロップテーブルの JSON データを非同期取得する。

    ブラウザネイティブの fetch API を PyScript の js モジュール経由で呼び出す。
    text() で文字列として受け取り、Python の json.loads() でパースすることで
    JS オブジェクトの変換問題を回避している。

    Args:
        url (str): 取得先 URL (warframestat.us API またはモックデータパス)

    Returns:
        dict: パース済みの JSON データ

    Raises:
        Exception: HTTP エラーまたはネットワークエラー時
    """
    # ブラウザの fetch を呼び出す (js モジュール経由)
    response = await js_fetch(url)

    if not response.ok:
        raise Exception(f"HTTP エラー: {response.status} {response.statusText}")

    # text() で受け取り Python の json.loads でパース (JS Proxy 変換を避けるため)
    text = await response.text()
    return json.loads(text)


# ============================================================
# データパース
# ============================================================

def classify_rarity(chance: float) -> str:
    """
    Intact 時の抽選確率からレアリティを再分類して返す。

    公式ドロップテーブルでは一部アイテムのレアリティが誤表記されているため、
    確率値を根拠に正しいレアリティを導出する。

    Args:
        chance (float): Intact 状態での 1 枠あたりの抽選確率

    Returns:
        str: "Common" / "Uncommon" / "Rare" / "Unknown"
    """
    for threshold, rarity in RARITY_BY_CHANCE.items():
        # 浮動小数点の誤差許容 (±0.01 の範囲で一致とみなす)
        if abs(chance - threshold) < 0.01:
            return rarity
    return "Unknown"


def extract_prime_item_and_part(item_name: str) -> tuple[str, str] | None:
    """
    アイテム名から「Prime アイテム名」と「パーツ名」を分離して返す。

    "Prime" を含まないアイテム (Forma など) は None を返してスキップする。

    Args:
        item_name (str): ドロップテーブル上のアイテム名
                         例: "Nova Prime Neuroptics Blueprint"

    Returns:
        tuple[str, str] | None:
            (prime_item_name, part_name) のタプル、または対象外の場合 None
    """
    if "Prime" not in item_name:
        return None

    match = PRIME_PART_PATTERN.match(item_name)
    if not match:
        return None

    prime_item_name = match.group(1).strip()
    part_name       = match.group(2).strip()
    return (prime_item_name, part_name)


def parse_relics(data: dict) -> dict:
    """
    ドロップテーブルの JSON データをパースし、
    Prime アイテムごとのレリックマッピングインデックスを構築する。

    処理フロー:
      1. Requiem レリックを除外
      2. "Intact" 状態のレリックのみを対象とする (確率ベースのレアリティ判定のため)
      3. 各報酬について Prime アイテムかどうかを判定
      4. Prime の場合、アイテム名・パーツ名・レアリティをインデックスに登録

    Args:
        data (dict): fetch_drop_table() が返した JSON データ

    Returns:
        dict: prime_index
              構造: {"Nova Prime": {"Blueprint": [{"tier": "Lith", ...}]}}
    """
    index: dict = {}
    relics: list = data.get("relics", [])

    for relic in relics:
        try:
            tier:       str  = relic.get("tier", "")
            relic_name: str  = relic.get("relicName", "")
            state:      str  = relic.get("state", "")
            rewards:    list = relic.get("rewards", [])

            # Requiem レリックは対象外のため除外
            if tier == "Requiem":
                continue
            if tier not in TIER_ORDER:
                continue
            # Intact 状態のみ対象 (確率でレアリティを再分類するため)
            if state != "Intact":
                continue

            for reward in rewards:
                try:
                    item_name: str   = reward.get("itemName", "")
                    chance:    float = float(reward.get("chance", 0.0))

                    result = extract_prime_item_and_part(item_name)
                    if result is None:
                        continue

                    prime_item_name, part_name = result
                    rarity = classify_rarity(chance)

                    if prime_item_name not in index:
                        index[prime_item_name] = {}
                    if part_name not in index[prime_item_name]:
                        index[prime_item_name][part_name] = []

                    relic_info = {
                        "tier":        tier,
                        "relicName":   relic_name,
                        "rarity":      rarity,
                        "displayName": f"{tier} {relic_name}",
                    }
                    index[prime_item_name][part_name].append(relic_info)

                except Exception as reward_err:
                    print(f"[警告] 報酬パースをスキップ: '{item_name}' - {reward_err}")
                    continue

        except Exception as relic_err:
            print(f"[警告] レリックパースをスキップ: {relic} - {relic_err}")
            continue

    return index


# ============================================================
# HTML スニペット生成
# ============================================================

def _part_sort_key(part_name: str) -> tuple[int, str]:
    """
    パーツ名のソートキーを返す。

    PART_PRIORITY に定義されたフレーム標準パーツを先頭に固定し、
    それ以外はアルファベット順とする。

    Args:
        part_name (str): ソート対象のパーツ名

    Returns:
        tuple[int, str]: (優先度インデックス, パーツ名) のタプル
    """
    try:
        return (PART_PRIORITY.index(part_name), part_name)
    except ValueError:
        return (len(PART_PRIORITY), part_name)


def render_relic_badge(relic_info: dict) -> str:
    """
    1 つのレリック情報からティアカラーのバッジ HTML を生成する。

    バッジの色はティア (Lith/Meso/Neo/Axi) で決まる。
    列がレアリティを表しているため、バッジはティアで色分けして情報量を最大化する。

    Args:
        relic_info (dict): {
            "tier":        str,  # 例: "Lith"
            "relicName":   str,  # 例: "N6"
            "rarity":      str,  # 例: "Common"
            "displayName": str,  # 例: "Lith N6"
        }

    Returns:
        str: バッジの HTML 文字列
    """
    tier        = relic_info.get("tier", "")
    display     = relic_info.get("displayName", "")
    css_classes = TIER_CSS.get(tier, "text-zinc-500 border-zinc-600 bg-zinc-900/40")

    return (
        f'<span class="inline-flex items-center border rounded px-2 py-1 '
        f'text-sm font-ui-badge font-medium tracking-wide whitespace-nowrap mr-1 mb-1 {css_classes}">'
        f'{display}'
        f'</span>'
    )


def render_relic_table(prime_item_name: str, index: dict) -> str:
    """
    選択された Prime アイテムのレリックマッピングマトリクス HTML を生成する。

    縦軸: パーツ名 (Blueprint 先頭固定、以下アルファベット順)
    横軸: レアリティ (Common / Uncommon / Rare)
    セル: 該当レリックのバッジ (ティア色で色分け、ティア順に並べる)

    Args:
        prime_item_name (str): 表示対象の Prime アイテム名 (例: "Nova Prime")
        index (dict):          parse_relics() が返した prime_index

    Returns:
        str: テーブルの HTML 文字列。アイテムが存在しない場合はメッセージ HTML。
    """
    if prime_item_name not in index:
        return (
            '<div class="flex flex-col items-center justify-center py-10 text-gray-400">'
            '<p class="font-ui-main text-sm">該当するアイテムが見つかりません。</p>'
            '</div>'
        )

    parts_data: dict    = index[prime_item_name]
    sorted_parts: list[str] = sorted(parts_data.keys(), key=_part_sort_key)

    # ── テーブルヘッダー行 (レアリティ列) ──
    rarity_headers_html = ""
    for rarity in RARITY_ORDER:
        header_css    = RARITY_HEADER_CSS.get(rarity, "text-zinc-400")
        chance_label  = RARITY_CHANCE_LABEL.get(rarity, "")
        rarity_headers_html += (
            f'<th class="px-4 py-3 text-center font-ui-main font-bold '
            f'{header_css} uppercase tracking-wide border-b border-gray-200 '
            f'whitespace-nowrap min-w-[180px] text-base">'
            f'{rarity}'
            f'<span class="block text-xs font-ui-main text-gray-500 '
            f'normal-case tracking-normal font-semibold mt-0.5">{chance_label}</span>'
            f'</th>'
        )

    # ── テーブルボディ行 ──
    rows_html = ""
    for part_name in sorted_parts:

        # レアリティごとにレリックを分類し、各列内でティア順 → レリック名順にソート
        relics_by_rarity: dict[str, list] = {rarity: [] for rarity in RARITY_ORDER}
        for relic_info in parts_data[part_name]:
            rarity = relic_info.get("rarity", "Unknown")
            if rarity in relics_by_rarity:
                relics_by_rarity[rarity].append(relic_info)

        for rarity in RARITY_ORDER:
            relics_by_rarity[rarity].sort(
                key=lambda rel: (
                    TIER_SORT_KEY.get(rel.get("tier", ""), 99),
                    rel.get("relicName", ""),
                )
            )

        cells_html = ""
        for rarity in RARITY_ORDER:
            relics = relics_by_rarity[rarity]
            if relics:
                badges = "".join(render_relic_badge(rel) for rel in relics)
                cells_html += (
                    f'<td class="px-4 py-2.5 align-top">'
                    f'<div class="flex flex-wrap">{badges}</div>'
                    f'</td>'
                )
            else:
                cells_html += (
                    '<td class="px-4 py-2.5 text-center text-gray-300 select-none text-lg">—</td>'
                )

        rows_html += (
            f'<tr class="border-b border-gray-200 hover:bg-gray-50 '
            f'transition-colors duration-100">'
            f'<td class="px-4 py-3 font-ui-main font-semibold text-gray-900 whitespace-nowrap '
            f'text-sm border-r border-gray-200">{part_name}</td>'
            f'{cells_html}'
            f'</tr>'
        )

    return (
        f'<div class="overflow-x-auto rounded-lg border border-gray-200 shadow-sm">'
        f'<table class="w-full text-sm text-gray-800 border-collapse">'
        f'<thead class="bg-gray-100">'
        f'<tr>'
        f'<th class="px-4 py-3 text-left text-sm font-ui-main font-semibold text-gray-600 '
        f'uppercase tracking-wide border-b border-gray-200 '
        f'border-r border-gray-200 whitespace-nowrap">パーツ</th>'
        f'{rarity_headers_html}'
        f'</tr>'
        f'</thead>'
        f'<tbody class="bg-white divide-y divide-gray-100">{rows_html}</tbody>'
        f'</table>'
        f'</div>'
    )


def render_item_options(items: list[str]) -> str:
    """
    Prime アイテム一覧から <select> の <option> タグ群を生成する。

    Args:
        items (list[str]): 表示する Prime アイテム名のリスト (ソート済みを推奨)

    Returns:
        str: <option> 要素の HTML 文字列
    """
    options = '<option value="">-- アイテムを選択 --</option>'
    for item in items:
        escaped = item.replace('"', "&quot;")
        options += f'<option value="{escaped}">{item}</option>'
    return options


# ============================================================
# UI 状態管理
# ============================================================

def set_loading_state(is_loading: bool) -> None:
    """
    ローディング状態に応じて全パネルの UI コンポーネントを切り替える。

    is_loading=True  → 全入力を disabled、スピナー表示
    is_loading=False → 全入力を有効化、スピナー非表示

    Args:
        is_loading (bool): True=ロード中, False=完了
    """
    loading_indicator = document.getElementById("loading-indicator")
    reload_icon       = document.getElementById("reload-icon")
    reload_btn        = document.getElementById("reload-btn")

    # 全パネルの入力要素を一括 disable/enable
    for panel_id in range(1, PANEL_COUNT + 1):
        search_input = document.getElementById(f"search-input-{panel_id}")
        item_select  = document.getElementById(f"item-select-{panel_id}")
        if is_loading:
            search_input.setAttribute("disabled", "true")
            item_select.setAttribute("disabled", "true")
        else:
            search_input.removeAttribute("disabled")
            item_select.removeAttribute("disabled")

    if is_loading:
        reload_btn.setAttribute("disabled", "true")
        loading_indicator.classList.remove("hidden")
        reload_icon.classList.add("hidden")
    else:
        reload_btn.removeAttribute("disabled")
        loading_indicator.classList.add("hidden")
        reload_icon.classList.remove("hidden")


def set_status(message: str, state: str = "normal") -> None:
    """
    ステータスバーにメッセージと色を設定する。

    Args:
        message (str): 表示するステータスメッセージ
        state   (str): "normal" | "success" | "error" | "loading"
    """
    status_text = document.getElementById("status-text")
    status_text.textContent = message

    color_map = {
        "normal":  "text-gray-500",
        "loading": "text-gray-500",
        "success": "text-yellow-400",
        "error":   "text-red-700",
    }
    for cls in color_map.values():
        status_text.classList.remove(cls)
    status_text.classList.add(color_map.get(state, "text-gray-500"))


def show_placeholder_in_result_area(panel_id: int) -> None:
    """
    アイテム未選択時のプレースホルダーを指定パネルの結果エリアに表示する。

    Args:
        panel_id (int): 対象パネルの番号 (1〜PANEL_COUNT)
    """
    result_area = document.getElementById(f"result-area-{panel_id}")
    result_area.innerHTML = (
        '<div class="flex flex-col items-center justify-center py-10 text-gray-300">'
        '<svg class="w-8 h-8 mb-2 opacity-60" fill="none" viewBox="0 0 24 24" stroke="currentColor">'
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1" '
        'd="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z"/>'
        '</svg>'
        '<p class="font-ui-main text-xs text-gray-400">アイテムを選択または検索してください</p>'
        '</div>'
    )


def show_error_in_all_panels(message: str) -> None:
    """
    全パネルの結果エリアにエラーメッセージを表示する。

    Args:
        message (str): 表示するエラーメッセージ
    """
    error_html = (
        '<div class="flex flex-col items-center justify-center py-10 gap-2">'
        '<div class="text-red-400 text-3xl select-none">⚠</div>'
        f'<p class="font-ui-main font-semibold text-red-700 text-xs">{message}</p>'
        '<p class="font-ui-main text-gray-400 text-xs">「データ更新」ボタンで再試行してください。</p>'
        '</div>'
    )
    for panel_id in range(1, PANEL_COUNT + 1):
        result_area = document.getElementById(f"result-area-{panel_id}")
        result_area.innerHTML = error_html


# ============================================================
# 結果表示
# ============================================================

def update_results(selected_item: str, panel_id: int) -> None:
    """
    選択された Prime アイテムのレリックテーブルを指定パネルに描画する。

    テーブル上部に PNG ダウンロードボタンを配置し、クリック時に
    js_window.downloadTableAsPng() を呼んでキャプチャ領域を PNG 出力する。
    ファイル名は "<Primeアイテム名>_レリックテーブル.png" の形式。

    Args:
        selected_item (str): 表示する Prime アイテム名。空文字でプレースホルダー。
        panel_id      (int): 描画対象パネルの番号 (1〜PANEL_COUNT)
    """
    if not selected_item:
        show_placeholder_in_result_area(panel_id)
        return

    result_area = document.getElementById(f"result-area-{panel_id}")
    table_html  = render_relic_table(selected_item, prime_index)

    # ダウンロードアイコン SVG (↓ 矢印)
    download_icon_svg = (
        '<svg class="w-3.5 h-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">'
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" '
        'd="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>'
        '</svg>'
    )

    # onclick 属性に JS を直接埋め込む。
    # PyScript の addEventListener 経由では Python → JS の関数プロキシが不安定なため、
    # 純粋な JS 呼び出しにすることで確実に動作させる。
    _capture_id = f"capture-area-{panel_id}"
    _filename   = f"{selected_item}_レリックテーブル"
    _onclick    = f"window.downloadTableAsPng('{_capture_id}', '{_filename}')"

    result_area.innerHTML = (
        # キャプチャ対象領域: アイテム名ラベル + テーブル
        f'<div id="{_capture_id}" class="bg-gray-50 p-3 rounded-lg">'
        f'<div class="mb-2">'
        f'<span class="font-ui-main font-bold text-base text-gray-900">{selected_item}</span>'
        f'<span class="font-ui-main text-gray-400 text-sm ml-2">のレリック一覧 (Intact)</span>'
        f'</div>'
        f'{table_html}'
        f'</div>'
        # ダウンロードボタン (キャプチャ範囲外 / テーブル下)
        f'<div class="mt-2 flex justify-end">'
        f'<button onclick="{_onclick}" '
        f'class="flex items-center gap-1.5 flex-shrink-0 '
        f'text-xs font-ui-main font-semibold '
        f'text-gray-500 hover:text-yellow-500 '
        f'border border-gray-300 hover:border-yellow-500 '
        f'bg-white rounded-lg px-3 py-1.5 '
        f'transition-all duration-150 shadow-sm">'
        f'{download_icon_svg}'
        f'PNG保存'
        f'</button>'
        f'</div>'
    )


# ============================================================
# データロード処理
# ============================================================

async def load_data(use_fallback: bool = False) -> None:
    """
    ドロップテーブルを非同期で Fetch し、パース・インデックス構築・UI 更新を行う。

    失敗した場合はフォールバック (モックデータ) で再試行し、
    それも失敗した場合のみエラー状態を表示する。

    Args:
        use_fallback (bool): True の場合、ローカルモックデータ (data_mock.json) を使用する
    """
    global prime_index, all_prime_items

    url          = FALLBACK_URL if use_fallback else DROP_TABLE_URL
    source_label = "モックデータ" if use_fallback else "warframestat.us"

    set_loading_state(True)
    set_status(f"{source_label} からデータを取得中...", "loading")

    try:
        data            = await fetch_drop_table(url)
        prime_index     = parse_relics(data)
        all_prime_items = sorted(prime_index.keys())

        # 全パネルのドロップダウンとプレースホルダーを更新
        options_html = render_item_options(all_prime_items)
        for panel_id in range(1, PANEL_COUNT + 1):
            item_select = document.getElementById(f"item-select-{panel_id}")
            item_select.innerHTML = options_html
            show_placeholder_in_result_area(panel_id)

        set_loading_state(False)
        set_status(
            f"{len(all_prime_items)} アイテムのデータを読み込み済み"
            f"{'（モックデータ）' if use_fallback else ''}",
            "success"
        )

    except Exception as fetch_err:
        print(f"[エラー] データ取得失敗 ({url}): {fetch_err}")
        set_loading_state(False)

        if not use_fallback:
            # API 失敗時はモックデータにフォールバック
            print("[情報] ローカルモックデータにフォールバックします...")
            set_status("API 取得失敗 — モックデータに切り替えます...", "error")
            await asyncio.sleep(1)  # ユーザーへの視覚的フィードバックのため少し待機
            await load_data(use_fallback=True)
        else:
            set_status("データの取得に失敗しました", "error")
            show_error_in_all_panels("データの取得に失敗しました。")


# ============================================================
# イベントハンドラ (3 パネル対応)
# ============================================================

def _make_search_handler(panel_id: int):
    """
    指定パネル用の検索入力ハンドラを生成して返す。

    @when デコレータは静的セレクタ文字列にしか使えないため、
    クロージャでパネル ID を束縛し、when() を関数として呼び出して動的登録する。

    Args:
        panel_id (int): 対象パネルの番号

    Returns:
        Callable: 検索入力イベントハンドラ関数
    """
    def on_search_input(event) -> None:
        """
        検索バーへの入力イベントハンドラ (パネル個別)。

        入力文字列で Prime アイテム名を部分一致で絞り込み、
        ドロップダウンをリアルタイム更新する。
        完全一致 1 件の場合は即座にテーブルを描画する。

        Args:
            event: DOM InputEvent
        """
        query: str = event.target.value.strip().lower()
        filtered   = [item for item in all_prime_items if query in item.lower()]

        item_select = document.getElementById(f"item-select-{panel_id}")
        item_select.innerHTML = render_item_options(filtered)

        if not query:
            show_placeholder_in_result_area(panel_id)
        elif len(filtered) == 1:
            # 絞り込み結果が 1 件なら即座に表示
            item_select.value = filtered[0]
            update_results(filtered[0], panel_id)
        else:
            show_placeholder_in_result_area(panel_id)

    return on_search_input


def _make_select_handler(panel_id: int):
    """
    指定パネル用のドロップダウン選択ハンドラを生成して返す。

    Args:
        panel_id (int): 対象パネルの番号

    Returns:
        Callable: 選択変更イベントハンドラ関数
    """
    def on_item_select(event) -> None:
        """
        アイテムドロップダウンの選択変更イベントハンドラ (パネル個別)。

        選択されたアイテムのレリックマトリクスを該当パネルに描画する。

        Args:
            event: DOM ChangeEvent
        """
        selected: str = event.target.value
        update_results(selected, panel_id)

        # 検索バーにも選択アイテム名を反映
        search_input = document.getElementById(f"search-input-{panel_id}")
        if selected:
            search_input.value = selected

    return on_item_select


# PyScript の when() を関数として呼び出し、各パネルにハンドラを動的登録する
# @when デコレータは静的セレクタにしか使えないため、ループで一括登録する
for _panel_id in range(1, PANEL_COUNT + 1):
    when("input",  f"#search-input-{_panel_id}")(_make_search_handler(_panel_id))
    when("change", f"#item-select-{_panel_id}")(_make_select_handler(_panel_id))


@when("click", "#reload-btn")
async def on_reload_click(event) -> None:
    """
    「データ更新」ボタンのクリックイベントハンドラ。

    全パネルの現在選択状態を保存してデータを再取得し、
    完了後に各パネルの選択状態を復元する。

    # PyScript の async ハンドラとして定義することで
    # データ取得の await が可能になる。
    """
    # 全パネルの現在選択を保存してから再取得
    saved_selections: list[str] = []
    for panel_id in range(1, PANEL_COUNT + 1):
        item_select = document.getElementById(f"item-select-{panel_id}")
        saved_selections.append(item_select.value)

    await load_data()

    # 再取得後に選択状態を復元
    for panel_id, saved_item in enumerate(saved_selections, start=1):
        if saved_item and saved_item in prime_index:
            item_select = document.getElementById(f"item-select-{panel_id}")
            item_select.value = saved_item
            update_results(saved_item, panel_id)


# ============================================================
# エントリーポイント
# ============================================================

async def main() -> None:
    """
    アプリケーション初期化のエントリーポイント。

    PyScript がこのファイルを読み込んだ際にトップレベル await で自動実行される。
    1. PyScript 起動オーバーレイを非表示
    2. ドロップテーブルの初回 Fetch・パース
    """
    # PyScript 起動オーバーレイを非表示にする
    overlay = document.getElementById("pyscript-loading-overlay")
    if overlay:
        overlay.style.opacity = "0"
        overlay.style.transition = "opacity 0.4s ease"
        await asyncio.sleep(0.4)
        overlay.remove()

    await load_data()


# PyScript (Pyodide バックエンド) はトップレベル await をサポートしている
await main()
