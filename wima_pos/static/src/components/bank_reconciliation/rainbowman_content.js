/** @odoo-module **/
import { BankRecFinishButtons } from "./finish_buttons";
import { Component, onWillUnmount } from "@odoo/owl";

export class BankRecRainbowContent extends Component {
    static template = "wima_pos.BankRecRainbowContent";
    static components = { BankRecFinishButtons };
    static props = {};

    setup() {
        onWillUnmount(() => {
            this.env.methods.initReconCounter();
        });
    }
}
