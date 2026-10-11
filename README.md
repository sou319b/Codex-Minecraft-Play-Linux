# Codex Minecraft Play Linux

A local MCP server that lets Codex see and control Minecraft on Ubuntu/GNOME Wayland. It captures an approved window and sends mouse and keyboard input through XDG Desktop Portals and PipeWire.

Supports screenshots, movement, clicks, key combinations, scrolling, and short action sequences. Runs as your desktop user; no API key is required by the server.

Inspired by [Codex-Minecraft-Gameplay](https://github.com/wz1119/Codex-Minecraft-Gameplay) by [wz1119](https://github.com/wz1119).

## Setup

Requires Ubuntu/GNOME Wayland, Python 3.10+, and a logged-in desktop session. Other desktop environments are untested.

Install dependencies:

```bash
sudo apt update
sudo apt install git python3-dbus python3-gi python3-pil \
  gir1.2-gstreamer-1.0 gir1.2-gtk-3.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  pipewire xdg-desktop-portal xdg-desktop-portal-gnome
```

Clone the repository and check your environment:

```bash
git clone https://github.com/sou319b/Codex-Minecraft-Play-Linux.git
cd Codex-Minecraft-Play-Linux
/usr/bin/python3 server.py doctor
```

The capture plugins reported by `doctor` should all be `true`.

Register the server with the Codex CLI from this directory:

```bash
codex mcp add linux-computer-use -- /usr/bin/python3 "$PWD/server.py" serve
```

Restart the MCP connection in Codex. For manual configuration, use [mcp-config.example.toml](mcp-config.example.toml) and replace the example path with your repository's absolute path.

## Usage

Open Minecraft, then ask Codex:

> Use linux-computer-use to connect to my Minecraft window and take a screenshot. Once I focus Minecraft, move forward for one second and show the result. Disconnect when finished.

Accept the OS sharing dialog within 45 seconds and select the Minecraft window. **Keep Minecraft focused: input goes to the currently focused app, even when sharing only one window.**

The usual flow is `linux_desktop_connect` → `linux_desktop_screenshot` → `linux_desktop_act` → `linux_desktop_disconnect`. Keep actions short; each hold is limited to five seconds. Coordinates use the latest screenshot's pixels. After an error, inspect the screen before repeating an action.

To stop input from another terminal:

```bash
/usr/bin/python3 server.py stop
```

After checking the screen, run `/usr/bin/python3 server.py reset-stop` to allow input again. Stop requests may wait for an in-flight capture or portal call to finish. To end sharing, ask Codex to disconnect or use the OS sharing indicator.

## Reading action results

`act` returns the image after releasing input, plus these fields:

| Field | Meaning |
| --- | --- |
| `input_completed` | The requested input and release finished; this does not prove the game objective succeeded. |
| `input_may_have_occurred` | On an interrupted action, some input may already have reached the application. Do not automatically repeat it. |
| `capture_fresh` | The returned image was obtained by a capture call after the attempted input. This is not a guarantee of source-frame age or focus. |
| `action_error`, `capture_error` | Input and observation failures are reported separately, including when both fail. |
| `timings` | `action_seconds` includes input and settling; `capture_seconds` includes capture, resize and PNG encoding; `total_seconds` covers those two stages. These exclude LLM time, result serialization and transport. |

An interrupted action gets at most one observation attempt while still connected.
Invalid actions are rejected before input or capture. A disconnected session is
not automatically reconnected. Always inspect the returned image and status
before deciding the next action.

`sequence` also returns `final_capture_metadata`, including the actual
`screenshot_size` of its returned PNG even if resizing stopped the sequence.
It does not start another capture once its time budget has expired;
`capture_skipped` explains this and `final_capture_fresh` may be false. In that
case, request a new screenshot before further input. An already-running capture
or synchronous portal call can still finish after the budget, so `max_seconds`
is not a hard real-time limit.

## Minecraft operation guidance

- Use short `act` calls while exploring. First turn and inspect; then move or
  mine. Mouse deltas are pointer movement units, not degrees: calibrate small
  turns after changing Minecraft sensitivity or mouse settings.
- Use `sequence` for already-inspected menus or predictable short actions.
  Choose `stable_regions` around fixed menu borders; exclude inventory slots,
  item counts, the cursor, and other content expected to change.
- A `progress` check detects pixel changes, not collected items or successful
  crafting. Water, grass, hand animation and cursor movement can change pixels
  without completing the intended action. Inspect the final screenshot.
- Keep screenshot width consistent when using menu coordinates. Capture is
  currently 5 fps; raising frame rate alone does not reduce model decision time.
- Only one controller should operate the game. Other agents can analyze code
  and results in parallel, but the server permits one input-owning session.

Version 0.3.2 fixes accumulated sleep during relative camera movement,
partial-action error feedback, capture after sequence budget expiry, and
modifier release order during disconnect. A diagnostic `probe` now refuses to
replace an existing session. `absolute_pointer_available` now also requires
an active session and pointer permission. Restart the MCP connection to load
code changes. A server sandbox must separately allow access to the desktop
session bus; restarting does not itself grant that access.

## 評価結果（2026-10-11、日本時間）

3つのサブエージェントによる実装監査と再レビュー、模擬入力テスト、
既存ベンチマークの分析、Minecraft 1.21.6の実画面試験を行いました。
評価は「短い操作と観察を交互に行う用途には使える。ただし、入力完了と
ゲーム内の目標達成は別に判定する必要がある」です。

例外時の入力解放、単一セッションの排他、window以外へのキャプチャ切替の拒否、
連続操作前の検証は良い土台です。修正後のlinux-computer-useは71テスト、
既存mcbenchは37テスト、合計108テストが成功しました。今回の回帰テストは16件です。

### 修正と効果

| 問題 | 改善 |
| --- | --- |
| 視点移動で各通知の通信時間に固定sleepが加算される | 単調時計で各区間の予定時刻を求め、通信に費やした時間を差し引く |
| 単発操作が途中で失敗すると部分入力と画面の情報が失われる | 部分実行の可能性・操作エラーを返し、接続中なら解放後に1回だけ撮影 |
| 連続操作の期限後にも新たな撮影を開始する | 撮影開始前に期限を確認し、撮影省略理由と最後の画像の寸法を返す |
| 操作処理と撮影の遅さを切り分けられない | `act.timings`で操作・撮影・合計を分離 |
| 接続中の診断で既存セッションを切る | `probe()`が既存接続を変更する前に拒否 |
| 切断時にShiftより後にマウスボタンを解放する | ボタンを先に解放し、修飾キーを保持 |
| 入力未許可でも絶対座標操作が利用可能と表示される | 接続とpointer権限も条件に追加 |

通知ごとに10msの遅延を入れた**模擬試験**では、1秒の視点移動を伴うW保持が
修正前1.510秒から修正後1.010秒になりました。実機の速度向上率ではありません。
同期通信そのものが遅い場合は、依然として指定時間を超過し得ます。

### 実画面で確認したこと

Ubuntu/GNOME Wayland、GStreamer 1.28.2、window共有、5fps、返却1280×720で実施しました。
通常MCPの接続はサンドボックス内からsession busへ接続できなかったため、
承認付きで同じサーバー実装をデスクトップ環境から呼び出しました。
初回は共有のみの許可で、再接続時に利用者がキーボードとマウスも許可しました。

| 計測 | 結果 | 範囲 |
| --- | --- | --- |
| 単独の画面取得7回 | 中央値0.200秒、0.165〜0.325秒 | メニュー等の異なる場面を含む |
| 単発操作11回 | 中央値0.574秒、0.321〜0.849秒 | 指定した保持時間・待機時間も異なる |
| 0.2秒の視点移動＋0.15秒待機、2回 | 操作処理はいずれも0.3505秒、撮影込み0.562／0.657秒 | LLM判断・クライアント配送・画像確認の時間を除く |
| 2段階のメニュークリック | 2.475秒で完了 | 固定領域を再確認してから各入力を実行 |

画面の変化から、視点移動、短いShift+W／Shift+S、インベントリの開閉、
ポーズ、保存してタイトルへ戻る動作を確認しました。体力減少は画面上では見られませんでした。
最初のメニュークリックはボタンをハイライトするだけで遷移しなかったため、
入力完了という返答だけでは成功判定できません。原因がフォーカスだったかは未確認です。
試験後は切断済みです。位置・視点の厳密な復元は測っていません。
元のウィンドウサイズへ戻すクリックは自動承認レビューに拒否されたため、最大化のままです。

### 残る制約と次の評価

- 共有対象がMinecraftでも入力先のフォーカスは検証できません。
- 視点のdx/dyは角度ではなく、感度に応じた小さな旋回で校正が必要です。
- 画素変化は採掘・回収・クラフトの成功を証明しません。今回これらの成功率は未測定です。
- キャプチャは5fpsで、元フレームの時刻を検証していません。厳密な映像の鮮度や
  通信中の即時停止は保証しません。
- 今回の共有では小さいウィンドウの周囲に黒い余白が入りました。最大化すると
  ゲーム部分を大きく読めました。黒い画素を自動で切り取るとゲーム内の暗部や
  座標対応を壊すおそれがあるため、今回は自動トリミングを導入していません。
- 既存の `mcbench` は静止画からの操作提案です。保存済み草原比較の正常回答6モデルは
  各1回で操作確定4.632〜29.087秒でしたが、実プレイの成功率を示す結果ではありません。
  この標本ではモデル判断待ちが大きいため、fpsだけを上げても全体の待ち時間は解消しません。

次は同じワールド・開始位置・感度で、目標への旋回、一定距離の移動、原木1個の採掘、
インベントリ移動、作業台作成を個別に反復し、成功率・補正回数・所要時間を測ると
操作基盤とLLM判断のどちらを改善すべきか切り分けられます。

計測値・呼び出し記録・コードのSHA-256は
[評価データ](evaluation/2026-10-11.json)に保存しました。画像自体はこのデータに含めていません。

## Development

Run the tests without sending desktop input:

```bash
/usr/bin/python3 -m unittest -v
```
