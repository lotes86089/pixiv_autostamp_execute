# Pixiv 自動貼圖桌面版(codex製作)

執行 `dist\PixivAutoStamp.exe` 後，填入 Pixiv `PHPSESSID` 與 Telegram 資訊，按下「啟動」即可。程式會每 30 秒檢查一次；按「停止」會在目前瀏覽器動作安全結束後停止。「關鍵字」及「排除作者」皆可每行輸入一項：程式會依序搜尋每個關鍵字；排除作者的新作品仍會發送通知，但不會執行貼圖。可按「儲存設定」只保存設定、不啟動服務。

## 檔案說明

- `config.json`：第一次啟動後自動建立，保存你的設定與憑證；請勿公開或提交。
- `seen_id.json`：已處理作品的記錄。
- `build_exe.bat`：重新打包 exe 的工具。

## 建置

在 Windows 安裝 Python 3.14 後，執行：

```bat
%LocalAppData%\Programs\Python\Python314\python.exe -m pip install -r requirements.txt
build_exe.bat
```

> 此工具透過 Chrome 自動化操作 Pixiv。Pixiv 介面更新後，貼圖按鈕的定位方式可能需要調整。

程式會自動偵測本機 Chrome 主版本，並下載對應的 ChromeDriver；Chrome 更新後首次啟動時可能需要數秒鐘完成此動作。
