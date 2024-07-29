/** @odoo-module */

import { Component } from "@odoo/owl";

export class AccountReportDebugPopover extends Component {
    static template = "wima_pos.AccountReportDebugPopover";
    static props = {
        close: Function,
        expressionsDetail: Array,
        onClose: Function,
    };
}
