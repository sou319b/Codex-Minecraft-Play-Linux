# Codex Minecraft Play Linux

Ubuntu / GNOME の Wayland 環境で、CodexなどのMCPクライアントから画面を見て、マウス・キーボードを操作するローカルMCPサーバーです。Minecraftの短い操作と画面確認を主な用途として開発しています。

A local stdio MCP server for Minecraft and desktop interaction on Ubuntu/GNOME Wayland, using user-approved XDG Desktop Portals and PipeWire.

XDG Desktop Portalの共有許可を使い、Waylandのまま動作します。サーバーの実行にroot権限、入力デバイスの権限変更、APIキーは不要です。OpenAI公式のComputer Useとは独立したツールです。

## できること

- 選択したウィンドウ、または明示的に指定したモニターのPNG画像を取得する。
- キーの同時押し、クリック、スクロール、相対・絶対マウス移動を送る。
- 移動＋ジャンプ、Shift＋クリックなどの複合入力と、結果の撮影を1回の呼び出しで行う。
- 最大16操作の短い操作列を実行し、画像の変化と時間予算を確認する。
- 別ターミナルから入力を停止する。

画像は毎秒5フレームで取得する設定です。「見る → 短い操作 → 見る」という使い方を想定しています。

## 動作環境

UbuntuのGUIセッションにログインしたユーザーとして起動してください。ヘッドレス環境や、デスクトップのD-Busに接続できないコンテナだけでは動きません。

必要な構成:

- Python 3.10以降（今回の検証は3.14.4）
- GNOME / Wayland、PipeWire
- XDG Desktop PortalのRemoteDesktop / ScreenCastに対応するバックエンド
- PythonのD-Bus、PyGObject、Pillowと、GStreamerプラグイン

Ubuntu / GNOME向けの依存パッケージ例:

```bash
sudo apt update
sudo apt install \
  python3-dbus python3-gi python3-pil \
  gir1.2-gstreamer-1.0 gir1.2-gtk-3.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  pipewire xdg-desktop-portal xdg-desktop-portal-gnome
```

インストールには管理者権限が必要ですが、サーバーは通常のユーザー権限で起動します。以下の手順は、aptで導入したモジュールを使える `/usr/bin/python3` を指定しています。pipだけでGStreamerやPortalのシステム依存を揃える構成にはしていません。

KDE、wlroots系、X11や他のディストリビューションの互換性は未検証です。バックエンドが必要なAPIや共有種別を提供しない場合は接続に失敗します。

## 導入と接続

### 1. ソースを取得する

```bash
git clone https://github.com/sou319b/Codex-Minecraft-Play-Linux.git
cd Codex-Minecraft-Play-Linux
```

### 2. 環境を確認する

```bash
/usr/bin/python3 server.py doctor
```

`session_type`、Portalの対応情報、`plugins` を確認してください。映像取得に必要なプラグインはすべて `true` である必要があります。このコマンドは画面共有も入力も開始しません。

共有開始前のセッション作成・共有設定・終了を確認する場合:

```bash
/usr/bin/python3 server.py probe
```

`sharing_started:false`、`input_sent:false` が返ります。これは実際の画面取得や入力の動作を保証するものではありません。

### 3. CodexへSTDIOサーバーを登録する

リポジトリのディレクトリで実行します。

```bash
codex mcp add linux-computer-use -- /usr/bin/python3 "$PWD/server.py" serve
codex mcp list
```

GUIから登録する場合も、名前は `linux-computer-use`、コマンドは `/usr/bin/python3`、引数は `server.py` の**絶対パス**と `serve` です。保存後にMCP接続を再起動してください。

手動設定の例は [mcp-config.example.toml](mcp-config.example.toml) にあります。例中のパスを実際の配置先へ置き換え、既存のCodex設定にMCP項目を追加します。既存設定の全体を置き換える必要はありません。設定方式は [Codexの公式MCPドキュメント](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) を参照してください。

### 4. ウィンドウの共有を許可する

Minecraftを開いてから、例えば次のようにCodexへ依頼します。

> linux-computer-useでMinecraftのウィンドウに接続し、画面を確認してください。私がMinecraftを手前にした後、1秒だけ前進して、結果を撮影してください。最後に共有を終了してください。

接続時にOSの共有許可ダイアログが開きます。既定は `source:"window"` です。対象ウィンドウを1つ選んで許可してください。ウィンドウ共有を確認できない場合は接続を終了し、モニター共有に自動で切り替えません。

**共有ウィンドウの選択は入力先の指定ではありません。入力は現在フォーカスされているアプリに届きます。操作対象を手前にしてから入力してください。**

共有開始の許可待ちは45秒でタイムアウトします。操作後は `linux_desktop_disconnect` で終了します。

## 画面取得だけを試す

```bash
/usr/bin/python3 server.py capture --output minecraft-preview.png
```

許可したウィンドウの画像を保存し、共有を終了します。キーやマウス入力は送信しません。

モニター全体を取得する場合は明示的に指定します。

```bash
/usr/bin/python3 server.py capture --source monitor --output desktop-preview.png
```

モニター共有では、他のアプリなど、そのモニターに表示された内容も取得します。ウィンドウ共有では、バックエンドによって対象ウィンドウの周囲に黒い余白が付く場合があります。

## MCPツール

すべてのツール名は `linux_desktop_` で始まります。

| ツール | 用途・主な引数 |
| --- | --- |
| `linux_desktop_connect` | 接続と共有許可。`source`: `window`（既定）または `monitor` |
| `linux_desktop_info` | 接続、入力許可、画像・論理座標のサイズを確認 |
| `linux_desktop_screenshot` | PNGと座標情報を返す。`max_width`: 320〜3840、既定1280 |
| `linux_desktop_move` | `x,y` の絶対移動、または `dx,dy` の相対移動 |
| `linux_desktop_click` | `button`: `left` / `right` / `middle`、`duration`、任意の `x,y` |
| `linux_desktop_key` | `keys` の同時押しと解放。例: `["w","space"]` |
| `linux_desktop_scroll` | `steps`: −20〜20。負数は上・左、正数は下・右。`axis`: `vertical` / `horizontal` |
| `linux_desktop_act` | 複合入力、解放、安定待ち、結果撮影 |
| `linux_desktop_sequence` | 短い操作列と、各操作後の画像確認 |
| `linux_desktop_disconnect` | 入力解放と共有終了 |

キー名は文字・数字、`enter`、`escape`（`esc`）、`space`、`ctrl`、`shift`、`alt`、`super`、`tab`、`backspace`、`up/down/left/right`、`F1`〜`F12` などです。キーボード配列とアプリ側の設定によって結果は異なります。

### 短い操作

以下はMCPの `tools/call` に渡す `name` と `arguments` の例です。

```json
{"name":"linux_desktop_connect","arguments":{"source":"window"}}
{"name":"linux_desktop_screenshot","arguments":{"max_width":1280}}
{"name":"linux_desktop_act","arguments":{"keys":["w","space"],"duration":0.3}}
{"name":"linux_desktop_act","arguments":{"dy":-40,"duration":0.2}}
{"name":"linux_desktop_act","arguments":{"buttons":["left"],"duration":0.6}}
{"name":"linux_desktop_disconnect","arguments":{}}
```

`act` はキー（最大8個）とボタン（最大3個）を押し、指定時間後に解放し、`settle` 秒待ってPNGを返します。`duration` は0.01〜5秒（既定0.08秒）、`settle` は0〜2秒（既定0.15秒）です。相対移動は短い間隔に分割して送ります。`move` の相対移動は既定0.15秒で、片方の軸を省略すると0です。

Minecraftの視点の変化量は感度・Raw Input設定などに依存します。小さな動きで確認してから調整してください。

### 座標クリックとドラッグ

`x,y` は**直前に返されたスクリーンショットのピクセル座標**です。サーバーがPortalの論理座標へ変換します。画像幅を変えた場合は、新しい画像から座標を読み直してください。

- 絶対座標には `x,y` の両方が必要です。
- Portalが論理サイズを返さない場合、絶対座標は利用できません。撮影後の `absolute_pointer_available` を確認してください。ポインターの入力許可も必要です。
- Shift＋クリックは、画像でメニューと対象を確認したうえで、同じ `act` に `keys:["shift"]`、`buttons:["left"]`、`x,y` を渡します。
- `act` は絶対座標への移動をボタンの押下より前に行います。ドラッグする場合は、先に `move` で開始点へ移動し、その後 `act` でボタンと相対移動を組み合わせます。
- 絶対座標と、0以外の相対移動を同じ `act` に混在させることはできません。

### 画面確認付きの操作列

`sequence` は1〜16操作で、時間予算 `max_seconds` は既定15秒、最大30秒です。全操作の引数・キー名・座標を入力前に検証し、各操作後に撮影します。

```json
{
  "name":"linux_desktop_sequence",
  "arguments":{
    "steps":[
      {"keys":["w"],"duration":0.3},
      {"dx":20,"duration":0.15}
    ],
    "max_seconds":5,
    "max_width":1280
  }
}
```

座標を使う操作には `stable_regions` が必須です。直前の画像で確認した、メニューの安定した縁などの矩形を `[左,上,右,下]` で指定します。右と下の端は含みません。1〜16領域を指定できます。

各座標操作の直前に撮影し、操作列開始時の画像と、各領域の平均RGB差を比較します。`max_mean_error` は既定8、範囲0〜30で、どれかの領域が閾値を超えると入力前に停止します。アイテム、ツールチップ、動く背景は安定領域に適しません。メニューを開く操作と、そのメニューの座標クリックは別の呼び出しにしてください。

**座標付きの操作列では、事前に撮影し、直前の画像と同じ `max_width` を使ってください。** サイズが一致しない場合は入力せず、新しい画像と停止理由を返します。

各操作の `progress` に `regions` と `min_mean_error` を指定すると、操作直前・直後の画像差が不足した場合に停止します。少なくとも1領域が指定値以上変わると成功扱いです。画像差は、アイテムの認識、移動距離や採掘数の証明にはなりません。

### エラーや途中停止の読み方

| 返却情報 | 意味 |
| --- | --- |
| `input_completed`（`act` / 各ステップ） | 指定した入力と解放が正常に終了したか |
| `capture_fresh`（`act`） | 入力後の画像を取得できたか |
| `status`（`sequence`） | `completed` または `stopped` |
| `completed_steps` | 入力と撮影、指定された進展検査まで成功した操作数 |
| `input_completed_steps` | 入力と解放まで正常に終了した操作数 |
| `final_capture_fresh` | 最後に試みた入力の後の画像が返っているか |
| `error` / `action_error` / `capture_error` | 停止や撮影失敗の理由 |

入力後の安定待ちや撮影に失敗しても、完了済みの入力は `input_completed:true` として保持します。`false` や値がない場合も、部分的な入力が届いた可能性があります。**エラー後に同じ操作を自動で再実行せず、画面を確認してから続きを判断してください。**

画像サイズ・座標対応の変化、安定領域の変化、進展不足、時間超過、入力・撮影エラーで残りの操作を中止します。撮影や同期D-Bus呼び出し自体を強制中断する仕組みはないため、時間予算を超えて返ることがあります。

## 停止と共有終了

別のターミナルで、入力の停止を要求できます。

```bash
/usr/bin/python3 server.py stop
```

停止状態はプロセスを再起動しても残ります。画面と入力状態を確認した後、再開する場合は次を実行します。

```bash
/usr/bin/python3 server.py reset-stop
```

停止要求は約20msごとの待機処理などで確認します。共有許可待ち、撮影、処理中の同期D-Bus呼び出しは即時には中断しません。`stop` は共有を終了するコマンドではありません。共有を終了するには `linux_desktop_disconnect`、OSの共有インジーケーター、またはサーバーの通常終了を使います。

正常終了・入力エラー・通常のSIGINT / SIGTERMでは押したキーとボタンの解放を試みます。個別の解放に失敗した場合は共有を閉じます。SIGKILLやOS障害時の終了処理は保証できません。

同じユーザーの本サーバーの操作接続は1つに制限します。ロックと停止状態は `/tmp/linux-computer-use-<uid>/` に保存します。他の遠隔操作ツールや、人間による入力までは排他制御しません。

## 制限とデータの扱い

- **入力先の自動フォーカス、フォーカス喪失時の自動停止、F8などの停止ホットキーは未実装です。** 返却される `focus_verified:false` はこの制限を示します。
- ウィンドウを共有しても、入力のアプリ単位の隔離はありません。
- IMEを使った文章入力、クリップボード連携、ゲーム内状態の認識は未実装です。
- 高DPI、ウィンドウの移動・拡大、モニター構成変更によって座標がずれる場合があります。共有をやり直し、画像と座標を確認してください。
- 最新のScreenCast仕様にある `pipewire-serial` によるストリーム接続は未対応で、PipeWireノードIDを使用します。モニターの着脱やサスペンド後は再接続してください。
- 人間と同じ速度で戦闘などをこなせることは保証していません。
- 許可の復元トークンは保存せず、新しい接続ごとにOSの許可を求めます。
- サーバーはネットワーク待受を開かず、MCPの標準入出力で画像と情報を返します。クライアントが画像をモデルへ送る場合、そのクライアント・サービスのデータ処理が適用されます。
- `capture` コマンドの画像は指定した場所へ保存します。MCPの撮影は画像を返すだけで、画像ファイルを保存しません。

## トラブルシューティング

| 症状 | 確認・対処 |
| --- | --- |
| `No module named dbus` / `gi` / `PIL` | 依存パッケージと `/usr/bin/python3` を確認 |
| `Cannot access the desktop session bus` | GUIにログインしたユーザーの通常のターミナルで起動。サンドボックスのD-Bus制限を確認 |
| 許可がタイムアウトした | 再接続し、OSのダイアログで45秒以内に許可 |
| `Portal did not confirm window-only capture` | バックエンドのウィンドウ共有対応を確認。モニター共有を意図する場合だけ `source:"monitor"` を指定 |
| 絶対座標が使えない | 撮影を行い、入力許可と論理サイズを確認。利用できなければ相対移動を使う |
| `No screen frame received within 8 seconds` | `doctor` のプラグイン、共有状態、PipeWireを確認し再接続 |
| `Input stopped` / `stop file` | 画面を確認して `reset-stop` を実行 |
| `Another linux-computer-use session` | 他の本サーバーの接続を終了する |
| ツールが更新されない | MCP接続を再起動し、必要なら新しいチャットで確認 |

## 構成と検証

```text
server.py                 MCP通信とCLI
portal.py                 Portalセッション、映像取得、入力、停止・排他制御
sequence.py               操作列、画像比較、時間予算、途中完了の報告
mcp-config.example.toml    Codex設定例
test_server.py           通信、入力、共有種別、GStreamer変換のテスト
test_sequence.py         操作列と画像チェックのテスト
test_review.py           停止、解放、座標、完了報告、ロックの回帰テスト
```

テストはリポジトリのディレクトリで実行します。

```bash
/usr/bin/python3 -m unittest -v
```

テストは偽のPortalと合成画像・映像を使用し、実際のデスクトップへ入力しません。GStreamerのPNG変換と、別プロセスとのMCP標準入出力通信は実処理を使います。

### 2026-10-08のレビュー

Ubuntu 26.04.1 LTS / GNOME / Wayland、Python 3.14.4、GStreamer 1.28.2で、`doctor` と共有開始前の `probe` が成功しました。RemoteDesktop v2、ScreenCast v5、必要な映像プラグインの存在を確認しています。

今回のレビューで次の3点を修正しました。

1. 入力完了後の安定待ちで停止した際、完了済みの入力が失われる報告を修正。`act` と `sequence` で完了情報を保持します。
2. 座標付き `sequence` が直前の画像と異なるサイズで開始される場合、入力前に停止するよう修正。
3. MCP標準入力の待機中もPortalの終了通知を処理し、共有終了後の操作ロックと映像資源を解放するよう修正。

修正後の自動テストは55件成功しました。今回、実画面の共有・キー入力・ゲーム操作は再検証していません。旧版のREADMEにはMinecraft 1.21.6での画像取得、移動、Shift＋クリック等の実機検証記録がありましたが、今回の修正後の動作保証とは区別しています。

## 参考資料

- [XDG RemoteDesktop API](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [XDG ScreenCast API](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [MCP stdio仕様](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [CodexのMCP接続](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex-Minecraft-Gameplay](https://github.com/wz1119/Codex-Minecraft-Gameplay): 複合入力・短い操作列・視覚的確認の設計参考。Windows用コードをそのまま使用するものではありません。
