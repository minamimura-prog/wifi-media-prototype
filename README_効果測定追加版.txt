Wi-Fi MEDIA 効果測定追加版

今回追加したもの
- 公開Webページを開くと「表示（impression）」を1件記録
- 「詳しく見る」を押すと「クリック（click）」を1件記録
- 店舗、広告ID、日時を記録
- 管理画面「効果測定」で累計表示、クリック、CTR、日別、店舗別を確認
- 個人を特定するIPアドレス等はこの試作では保存しません

ローカル起動
start.command をダブルクリック

Renderへの反映
1. このフォルダのファイルをGitHubの wifi-media-prototype リポジトリへアップロードしてCommit
2. Renderが自動デプロイする設定なら数分待つ
3. https://wifi-media-prototype.onrender.com/ を開く
4. /admin の「効果測定」で実測値を確認

注意
Render Free のローカルファイル保存は本番向けの恒久データベースではありません。実運用ではPostgreSQL等の外部DBへ移行してください。
