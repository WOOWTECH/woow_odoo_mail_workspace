/** @odoo-module **/
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { FormViewDialog } from "@web/views/view_dialogs/form_view_dialog";

export class MailWorkspace extends Component {
    static template = "woow_mail_workspace.Workspace";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState({
            scope: "personal", box: "inbox", search: "", unread: false, folderId: false,
            messages: [], selected: [], detail: null, offset: 0, more: false,
            loading: false, opening: false, failed: false, sidebar: false,
            admin: false, folders: [], draftCount: 0, name: "", busy: false,
        });
        this.listVersion = 0;
        this.detailVersion = 0;
        this.alive = true;
        this.boxes = [
            { id: "inbox", label: "收件匣", icon: "inbox" },
            { id: "starred", label: "已加星號", icon: "star-o" },
            { id: "sent", label: "寄件紀錄", icon: "paper-plane-o" },
            { id: "drafts", label: "草稿", icon: "file-text-o" },
            { id: "archive", label: "封存", icon: "archive" },
            { id: "all", label: "全部郵件", icon: "envelope-o" },
            { id: "trash", label: "垃圾桶", icon: "trash-o" },
        ];
        onWillStart(async () => {
            try { await this.bootstrap(); await this.load(); }
            catch (error) { this.fail(error); }
        });
        onWillUnmount(() => { this.alive = false; this.listVersion++; this.detailVersion++; });
    }

    async rpc(method, args = [], kwargs = {}) {
        return this.orm.call("woow.mail.workspace", method, args, kwargs);
    }
    fail(error) {
        this.notification.add(error?.data?.message || error?.message || "操作失敗，請重試。", { type: "danger" });
    }
    async bootstrap() {
        const data = await this.rpc("bootstrap");
        if (this.alive) Object.assign(this.state, { admin: data.admin, folders: data.folders,
            draftCount: data.draft_count, name: data.name });
    }
    async load() {
        const version = ++this.listVersion;
        this.state.loading = true;
        this.state.failed = false;
        try {
            let data;
            if (this.state.box === "drafts") {
                const domain = [["state", "=", "draft"]];
                if (this.state.search.trim()) domain.push(["subject", "ilike", this.state.search.trim()]);
                const drafts = await this.orm.searchRead("woow.mail.draft", domain,
                    ["subject", "write_date", "partner_ids"], { offset: this.state.offset, limit: 41 });
                data = { messages: drafts.slice(0, 40).map(d => ({ ...d, date: d.write_date,
                    sender: "草稿", preview: "點選繼續撰寫", is_read: true })), has_more: drafts.length > 40 };
            } else {
                data = await this.rpc("list_messages", [], { scope: this.state.scope, box: this.state.box,
                    search: this.state.search, offset: this.state.offset, unread: this.state.unread,
                    folder_id: this.state.folderId });
            }
            if (this.alive && version === this.listVersion) {
                this.state.messages = data.messages;
                this.state.more = data.has_more;
                this.state.selected = [];
            }
        } catch (error) {
            if (this.alive && version === this.listVersion) {
                this.state.messages = []; this.state.failed = true; this.fail(error);
            }
        } finally {
            if (this.alive && version === this.listVersion) this.state.loading = false;
        }
    }
    get scopeLabel() {
        return { personal: "我的郵件", accessible: "可存取的郵件", database: "資料庫郵件" }[this.state.scope];
    }
    get title() {
        return this.state.box === "folder" ? this.state.folders.find(f => f.id === this.state.folderId)?.name
            : this.boxes.find(b => b.id === this.state.box)?.label;
    }
    get selectedAll() {
        return this.state.messages.length > 0 && this.state.messages.every(m => this.state.selected.includes(m.id));
    }
    clearDetail() { this.detailVersion++; this.state.detail = null; this.state.opening = false; }
    async navigate(box, folderId = false) {
        this.state.box = box; this.state.folderId = folderId; this.state.offset = 0;
        this.state.sidebar = false; this.state.selected = []; this.clearDetail();
        await this.load();
    }
    async changeScope(event) {
        this.state.scope = event.target.value; this.state.offset = 0;
        this.state.selected = []; this.clearDetail(); await this.load();
    }
    async search(event) { event.preventDefault(); this.state.offset = 0; this.clearDetail(); await this.load(); }
    async refresh() { await this.bootstrap(); this.clearDetail(); await this.load(); }
    async toggleUnread() { this.state.unread = !this.state.unread; this.state.offset = 0; await this.load(); }
    async page(delta) { this.state.offset = Math.max(0, this.state.offset + delta * 40); this.clearDetail(); await this.load(); }
    select(id) {
        this.state.selected = this.state.selected.includes(id)
            ? this.state.selected.filter(i => i !== id) : [...this.state.selected, id];
    }
    selectAll() { this.state.selected = this.selectedAll ? [] : this.state.messages.map(m => m.id); }
    async open(message) {
        if (this.state.box === "drafts") { this.openDraft(message.id); return; }
        const version = ++this.detailVersion;
        const scope = this.state.scope;
        this.state.opening = true; this.state.detail = null;
        try {
            const detail = await this.rpc("get_message", [message.id, scope]);
            if (!this.alive || version !== this.detailVersion) return;
            this.state.detail = detail;
            if (!detail.is_read) {
                await this.rpc("set_state", [[message.id], { is_read: true }, scope]);
                if (this.alive && version === this.detailVersion) { detail.is_read = true; message.is_read = true; }
            }
        } catch (error) { if (this.alive && version === this.detailVersion) this.fail(error); }
        finally { if (this.alive && version === this.detailVersion) this.state.opening = false; }
    }
    async update(values, ids = null) {
        ids = ids || (this.state.selected.length ? [...this.state.selected] : this.state.detail ? [this.state.detail.id] : []);
        if (!ids.length || this.state.busy) return;
        this.state.busy = true;
        try {
            await this.rpc("set_state", [ids, values, this.state.scope]);
            if (this.state.detail && ids.includes(this.state.detail.id)) Object.assign(this.state.detail, values);
            if ("trashed" in values || "archived" in values || "folder_id" in values) this.clearDetail();
            await this.load();
        } catch (error) { this.fail(error); }
        finally { this.state.busy = false; }
    }
    async star(message) { await this.update({ starred: !message.starred }, [message.id]); }
    async move(event) {
        if (event.target.value === "") return;
        const value = Number(event.target.value);
        await this.update({ folder_id: value || false, archived: false, trashed: false });
        event.target.value = "";
    }
    async compose(mode = "new") {
        if (this.state.busy) return;
        this.state.busy = true;
        try {
            const action = await this.rpc("compose", [this.state.detail?.id || false, mode, this.state.scope]);
            this.openDraft(action.res_id);
        } catch (error) { this.fail(error); }
        finally { this.state.busy = false; }
    }
    openDraft(id) {
        this.dialog.add(FormViewDialog, { resModel: "woow.mail.draft", resId: id, title: "撰寫郵件",
            onRecordSaved: () => this.refresh() }, { onClose: () => this.refresh() });
    }
    newFolder() {
        this.dialog.add(FormViewDialog, { resModel: "woow.mail.folder", title: "新增資料夾",
            onRecordSaved: () => this.bootstrap() });
    }
    manageFolders() {
        this.action.doAction({ type: "ir.actions.act_window", name: "我的資料夾",
            res_model: "woow.mail.folder", views: [[false, "list"], [false, "form"]], target: "current" });
    }
    async openRecord() {
        try { await this.action.doAction(await this.rpc("open_record", [this.state.detail.id, this.state.scope])); }
        catch (error) { this.fail(error); }
    }
    date(value) {
        if (!value) return "";
        const d = new Date(value.replace(" ", "T") + "Z");
        return Number.isNaN(d.valueOf()) ? value : new Intl.DateTimeFormat("zh-TW", {
            month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
        }).format(d);
    }
    initials(sender) { return (sender || "?").replace(/[<"']/g, "").trim().slice(0, 1).toUpperCase(); }
    get emailDocument() {
        // A separate opaque-origin document blocks scripts, forms, remote pixels,
        // remote CSS and framing. Relative URLs cannot access the Odoo origin.
        return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><style>body{font:14px/1.8 system-ui,sans-serif;color:#263747;margin:0;padding:4px 2px;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%}blockquote{border-left:3px solid #dce5ef;padding-left:14px;color:#66768a;margin-left:0}a{color:#1767b3}</style></head><body>${this.state.detail?.body || ""}</body></html>`;
    }
}
registry.category("actions").add("woow_mail_workspace.Workspace", MailWorkspace);
