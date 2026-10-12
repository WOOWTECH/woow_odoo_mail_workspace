{
    "name": "WooW Mail Workspace",
    "summary": "A clean, personal workspace for existing Odoo business email",
    "version": "18.0.1.0.1",
    "category": "Productivity/Discuss",
    "author": "WooW Tech",
    "license": "LGPL-3",
    "depends": ["mail", "web"],
    "data": ["security/security.xml", "security/ir.model.access.csv", "views/workspace_views.xml"],
    "assets": {
        "web.assets_backend": [
            "woow_mail_workspace/static/src/workspace.js",
            "woow_mail_workspace/static/src/workspace.xml",
            "woow_mail_workspace/static/src/workspace.scss",
        ],
    },
    "application": True,
    "installable": True,
}
