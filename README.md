# Pixiv 自動貼圖桌面版(codex製作)

執行 `dist\PixivAutoStamp.exe` 後，填入 Pixiv `PHPSESSID` 與 Telegram 資訊，按下「啟動」即可。程式會每 30 秒檢查一次每個關鍵字 API 第一頁的全部作品；只有評論數為 0 的新作品才會貼圖，已有評論的作品會通知 Telegram 並略過。按「停止」會在目前瀏覽器動作安全結束後停止。按右上角叉號會隱藏到系統匣，服務仍會在背景繼續執行；可在系統匣圖示選擇「顯示視窗」或「結束程式」。程式同時只允許一個執行個體，重複開啟時會還原既有視窗至前景。「關鍵字」及「排除 userId」皆可每行輸入一項：程式會依序搜尋每個關鍵字；「查看 API 結果」會開啟相同查詢網址，「添加API結果」會在服務停止時清除舊 ID，再將目前第一頁全部作品標記為已處理。「檢查活動」可於服務停止時掃描一個排除 userId 在一個關鍵字第一頁作品的所有留言與貼圖（包括回覆）；同一 userId 自己上傳的作品不會列入，結果會依作品上傳時間由新到舊列出。排除 userId 的新作品仍會發送通知，但不會執行貼圖。可按「儲存設定」只保存設定、不啟動服務。

## 檔案說明

- `config.json`：第一次啟動後自動建立，保存你的設定與憑證；請勿公開或提交。
- `seen_id.json`：已處理作品的記錄，最多保留 1,000 筆。
- `pixiv_autostamp_icon.png`、`pixiv_autostamp_icon.ico`：程式、系統匣與 EXE 使用的圖示資源。
- `build_exe.bat`：重新打包 exe 的工具。

## 建置

在 Windows 安裝 Python 3.14 後，執行：

```bat
%LocalAppData%\Programs\Python\Python314\python.exe -m pip install -r requirements.txt
build_exe.bat
```

> 此工具透過 Chrome 自動化操作 Pixiv。Pixiv 介面更新後，貼圖按鈕的定位方式可能需要調整。

程式會自動偵測本機 Chrome 主版本，並下載對應的 ChromeDriver；Chrome 更新後首次啟動時可能需要數秒鐘完成此動作。
