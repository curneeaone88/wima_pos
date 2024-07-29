/** @odoo-module */

import { AccountReport } from "@wima_pos/components/account_report/account_report";
import { AccountReportFilters } from "@wima_pos/components/account_report/filters/filters";

export class MulticurrencyRevaluationReportFilters extends AccountReportFilters {
    static template = "wima_pos.MulticurrencyRevaluationReportFilters";

    //------------------------------------------------------------------------------------------------------------------
    // Custom filters
    //------------------------------------------------------------------------------------------------------------------
    async filterExchangeRate() {
        Object.values(this.controller.options.currency_rates).forEach((currencyRate) => {
            const input = document.querySelector(`input[name="${ currencyRate.currency_id }"]`);

            currencyRate.rate = input.value;
        });

        this.controller.reload('currency_rates', this.controller.options);
    }
}

AccountReport.registerCustomComponent(MulticurrencyRevaluationReportFilters);
