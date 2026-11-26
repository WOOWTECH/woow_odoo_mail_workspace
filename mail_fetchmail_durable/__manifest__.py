# -*- coding: utf-8 -*-
{
    "name": "Mail Fetchmail Durable Failure Ledger",
    "summary": "Durable incoming mail failure tracking, explicit retry, and strict mail TLS helpers",
    "version": "18.0.1.0.1",
    "category": "Discuss",
    "license": "LGPL-3",
    "author": "WooW Tech Cluster",
    "depends": ["mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/mail_fetch_failure_views.xml",
    ],
    "post_load": "post_load",
    "installable": True,
    "application": False,
}
