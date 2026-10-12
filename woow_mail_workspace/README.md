# WooW Mail Workspace — Odoo 18

原生 Odoo 後台郵件工作區。沿用現有 bridge → mailcow → durable fetchmail →
`mail.message` 與 Odoo 寄件佇列，不另設 IMAP 消費者、不安裝外部 webmail。
OWL client action、QWeb、Odoo 圖示、原生 HTML 編輯器、收件者及附件元件。
版面採 Odoo 紫色主題、原生 Bootstrap 表格／按鈕／欄位／卡片樣式；
預設全寬清單，點信後展開右側閱讀區，手機使用單欄。郵件清單仍為自訂 OWL
元件，保留原有權限與隔離的閱讀方式；撰寫與資料夾管理沿用原生 Odoo 表單。

## 功能及權限

| 入口 | 可見範圍 |
| --- | --- |
| 我的郵件（預設） | 本人寄出、列為收件者或收到 Odoo 通知的業務郵件，仍須通過原生郵件存取檢查 |
| 可存取的郵件 | 依 Odoo 原生郵件及業務文件權限可讀的郵件 |
| 資料庫郵件 | 僅 `base.group_system`；明確選取後讀取本資料庫業務郵件，不跨資料庫 |

三個入口均排除內部備註、Discuss 聊天與系統事件。管理入口不會把所有信再寄给管理員。
Odoo 資料庫若本來跨公司使用，管理入口會涵蓋該資料庫所有公司的業務郵件；一般入口遵循原生公司權限。

- 收件匣、寄件紀錄、星號、草稿、封存、垃圾桶、個人資料夾。
- 主旨、寄件者、收件者 email、內文搜尋；40 封一頁與未讀篩選。
- 讀取本文、附件下載、回到原業務紀錄；桌面雙欄與手機單欄閱讀。
- 原生收件者元件、HTML 編輯器、附件上傳、儲存／捨棄草稿、寄送、回覆、轉寄。
- 已讀、星號、移動及垃圾桶只寫入使用者自己的 `woow.mail.state`。
  **不改原始 `mail.message`、`mail.notification.is_read` 或 IMAP UNSEEN**；垃圾桶不是實體刪信。
- 回覆保留原業務對話，要求對原紀錄有寫入權限，沿用原生追蹤者通知規則。
  沒有回覆權限時仍可轉寄自己能讀的信。管理入口的讀取權不會變成原紀錄寫入權。
- 新信／轉寄建立僅寄件者與初始收件者可存取的 `woow.mail.conversation`；收件同仁可在同一對話回覆。
  對話成員不可任意改寫，亦受公司範圍限制。轉寄複製附件，不移動原附件。
- 送出只交給 Odoo 原生 composer／佇列，沒有新 SMTP 設定、直接寄信或背景同步排程。
  使用者通知方式仍依 Odoo 設定；Odoo 內部通知不必然產生 SMTP 信件。
  同一草稿用資料庫列鎖防止重複按送出，寄件紀錄不代表外部收件者已收到。

## 安全邊界

- 所有 RPC 參數視為不可信；資料庫管理範圍在伺服器檢查群組，不能靠隱藏按鈕授權。
- `sudo` 僅用於明確授權的資料庫郵件讀取、個人狀態表及已驗證附件的檔案串流。
  草稿與業務紀錄寫入仍遵循使用者權限。
- 草稿、資料夾受全域 owner record rule 保護；新對話限寄件者與初始收件者；禁止偽造 owner、來源或送出狀態。
  `woow.mail.state` 刻意不給使用者直接 ACL，安裝時的「no access rules」警告屬預期。
- 本文經 Odoo HTML sanitizer，並放入 opaque-origin sandbox iframe；CSP 阻擋 script、表單、
  遠端圖片、遠端樣式。回覆與轉寄引文轉成文字，避免原生編輯器載入追蹤圖片。
- 附件下載每次驗證訊息權限及附件成員關係，禁止使用任意 attachment ID。

## 目前不宣稱的能力

這是 **既有 Odoo 郵件的 GUI**。mailcow 中尚未進入 Odoo 的原始郵件不會憑空出現，
個人資料夾與已讀狀態也不會雙向同步到 IMAP／Gmail。不要讓這個套件接手 ingest 信箱。

尚未包含 CC/BCC、排程寄送、撤回、完整 RFC822 檢視、IMAP 資料夾同步、信件串折疊、
垃圾郵件分類、附件全文索引、草稿自動儲存、範本選擇、外部圖片解封或 CID 行內圖片重組。
收件者使用原生聯絡人選取／建立權限；不提升一般使用者的聯絡人維護權限。
本文中的連結亦受 sandbox 限制。回覆引用保留文字，沒有保留原始排版。

正確分頁先取得已授權的訊息 ID，再取當頁正文；原生 `mail.message._search` 是先 SQL 分頁、
再做文件權限過濾，直接 limit 會漏頁。大量郵件的效能／記憶體測試仍是商用放行前必要項目。
目前不是有規模驗證的郵件搜尋服務。

## 安裝與回滾邊界

適用 Odoo 18，依賴 `mail`、`web`。放進既有 addons path、更新 App 清單後安裝
`woow_mail_workspace`；只會新增本模組的 models/views/menu/assets，不更改租戶 mail server、
fetchmail、bridge、CronJob 或 NetworkPolicy。實際生產安裝須另外決定及執行備份／資源檢查。

卸載會刪除個人整理狀態、資料夾、草稿與本模組對話資料，因此**不可把卸載視為無損回滾**。
測試失敗時先停用入口／還原程式與資料庫備份；保留寄出信與稽核紀錄。
既有郵件資料不做遷移、不做回填、不做 purge。

## 驗證

Odoo 原生 `TransactionCase` 測試放在 `tests/test_workspace.py`。只能對隔離測試資料庫執行：

```text
odoo -d <isolated_db> -i woow_mail_workspace --test-enable \
  --test-tags /woow_mail_workspace --stop-after-init --no-http --max-cron-threads=0
```

測試資料使用 `.invalid` email，實際送出流程的測試將 `ir.mail_server.send_email` 設成一呼叫就失敗。
分頁測試亦使用相同資料跑「先 limit 再驗權限」的舊路徑，確認回傳不足 40 封，避免空轉。
本機瀏覽器驗證腳本、原始日誌與示意資料截圖在 `acceptance/mail-workspace-20261012/`。
該腳本會修改指定的合成資料，**不是唯讀檢查，禁止指向生產**。

## 研究來源（2026-10-12）

- [Odoo 18 client actions](https://www.odoo.com/documentation/18.0/developer/howtos/javascript_client_action.html)：原生 OWL action 與後台 registry。
- [Odoo 18 mail.message 原始碼](https://github.com/odoo/odoo/blob/18.0/addons/mail/models/mail_message.py)：訊息可见性、收件人與文件權限。
- [Odoo 18 composer 原始碼](https://github.com/odoo/odoo/blob/18.0/addons/mail/wizard/mail_compose_message.py)：沿用原生寄件、附件與對話機制。
- [Kanak Odoo Inbox](https://apps.odoo.com/apps/modules/18.0/odoo_inbox)：Odoo 內郵件的集中式介面；本身不是完整外部 IMAP 信箱。
- [Cetmix Mail Messages 文件](https://cetmix.com/fr/documentation/messages)：以文件權限、郵件瀏覽、回覆／轉寄及個人整理為參考。
- [Gmail filters help](https://support.google.com/mail/answer/6579?hl=en)：搜尋與整理的互動參考。

未取得、嵌入或改寫上述商業套件的私有程式碼；未使用 Gmail 圖片、商標或第三方設計素材。
