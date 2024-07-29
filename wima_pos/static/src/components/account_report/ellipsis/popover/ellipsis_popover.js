/** @odoo-module */

import { Component } from "@odoo/owl";

export class AccountReportEllipsisPopover extends Component {
    static template = "wima_pos.AccountReportEllipsisPopover";
    static props = {
        close: Function,
        name: String,
        copyEllipsisText: Function,
    };
}
