/** @odoo-module **/

import { startWebClient } from "@web/start";
import { WebClientMenu } from "./webclient/webclient";

/**
 * This file starts the menu webclient. In the manifest, it replaces
 * the community main.js to load a different webclient class
 * (WebClientMenu instead of WebClient)
 */
startWebClient(WebClientMenu);
