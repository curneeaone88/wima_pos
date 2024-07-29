odoo.define('custom_receipts_for_pos.pos_custom', function (require) {
    "use strict";

    var models = require('point_of_sale.models');
    var OrderSuper = models.Order;

    models.load_fields('pos.order', ['total_items']);

    models.Order = models.Order.extend({
        export_for_printing: function(){
            var json = OrderSuper.prototype.export_for_printing.apply(this, arguments);
            json.total_items = this.get_total_items();
            return json;
        },
        get_total_items: function() {
            var total = 0;
            this.orderlines.each(function(line){
                total += line.get_quantity();
            });
            return total;
        },
    });
});
