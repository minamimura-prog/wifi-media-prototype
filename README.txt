Wi-Fi MEDIA Prototype v3

公開ページ: /
管理画面: /admin
機能: レスポンシブ、Web全体デザイン変更、広告管理、店舗管理、効果測定、過去比較、円グラフ、GIF/JPG/PNG/WebP、外部リンク。

ローカル: pip install -r requirements.txt && python server.py
PCとスマホを同じWi-Fiに接続し、http://PCのIP:5000/ でスマホ確認できます。

インターネット公開: Render等のPython Web Serviceへデプロイ可能。公開後は https://xxxxx.onrender.com/ のようなURLになります。
このチャットから外部ホスティングアカウントへログインして公開URLを発行することはできないため、デプロイ用一式を同梱しています。
本番化にはDB、画像ストレージ、認証、HTTPS、アクセス解析、店舗ごとの権限管理が必要です。
