# WooW Odoo Mail Workspace

Odoo 18 原生後台郵件工作區。將已存在 Odoo 中的業務郵件集中呈現，沿用原生寄件、附件與文件權限。

- Odoo 紫色主題、表格清單、右側閱讀區，支援手機單欄。
- 我的郵件／可存取郵件／管理員資料庫郵件三個獨立入口。
- 搜尋、星號、已讀、個人資料夾、封存、垃圾桶。
- 原生表單撰寫、儲存草稿、回覆、轉寄及附件。
- 信件 HTML 使用隔離 iframe 與 CSP；附件下載驗證郵件與附件權限。

**這是 Odoo 既有郵件的操作介面，尚未提供完整個人 IMAP/Gmail 信箱綁定或雙向同步。**
寄件紀錄不等於收件者已收到；實際寄送時間取決於 Odoo 的郵件排程。

## 安裝

適用 Odoo **18**，模組版本 **18.0.1.0.1**，依賴 `mail`、`web`。

1. 將本 repo 放到主機，將 repo 根目錄加入 Odoo `addons_path`；或將 `woow_mail_workspace/` 複製到既有 addons 目錄。
2. 更新應用程式清單，安裝 **WooW Mail Workspace**。
3. 從 Odoo 應用程式選單開啟「郵件」。

收信與寄信伺服器需由既有環境配置；本模組不代建 SMTP/IMAP 帳號或修改寄送排程。
正式環境請先備份並驗證於隔離資料庫。卸載會刪除模組的個人狀態、草稿、資料夾與對話，不能當作無損回滾。

## 文件與測試

- [功能、權限與已知限制](woow_mail_workspace/README.md)
- [套件內 Odoo 測試](woow_mail_workspace/tests/test_workspace.py)
- [目前驗證紀錄摘要](VALIDATION.md)

套件 README 中的 `acceptance/` 路徑屬於原部署工作區，不隨 repo 發布；部署腳本、實際環境憑證、資料庫與原始測試紀錄均不收錄。

## 授權

LGPL-3.0。詳見 [COPYING.LESSER](COPYING.LESSER) 與其引用的 [COPYING](COPYING)。
