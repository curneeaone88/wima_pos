/** @odoo-module **/

import { WebClient } from "@web/webclient/webclient";
import { useService } from "@web/core/utils/hooks";
import { MenuNavBar } from "./navbar/navbar";

export class WebClientMenu extends WebClient {
    setup() {
        super.setup();
        this.hm = useService("home_menu");
    }
    _loadDefaultApp() {
        return this.hm.toggle(true);
    }
}
WebClientMenu.components = { ...WebClient.components, NavBar: MenuNavBar };
