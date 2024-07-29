from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError
from jinja2 import Template


class ExternalTemplate(models.Model):
    _name = 'external.template'
    _description = 'Custom template McEasy'

    name = fields.Char(string="Name", required=True)
    description = fields.Text(string="Description")
    model_id = fields.Many2one("ir.model", string="Model")
    model = fields.Char(related="model_id.model")
    document_format = fields.Char(string="Document Format")
    content = fields.Html()
    is_default = fields.Boolean(string="Default", copy=False)
    active = fields.Boolean(default=True)

    @api.onchange("is_default")
    def onchange_is_default(self):
        res = self.search([('model_id','=', self.model_id.id)])
        if not self.active:
            if not self.is_default or len(res) == 0:
                return
            self.is_default = False
            raise ValidationError("Mohon untuk mengcentang active terlebih dahulu")
        for rec in res:
            rec.is_default = False

    @api.onchange("active")
    def onchange_active(self):
        for rec in self:
            if not rec.active:
                rec.is_default = False

    def preview_action(self):
        return {
            'name': _('Preview Report'),
            'views': [[self.env.ref('wima_pos.wizard_external_template_view_form').id, "form"]],
            'res_model': 'wizard.external.template',
            'type': 'ir.actions.act_window',
            'context' : {'default_external_template_id': self.id},
            'target': 'new',
        }

    def render_template(self, data):
        if not data:
            raise ValidationError("render_template need data record")
        if not self.content:
            return ""

        content = self.content.replace("%7B","{")
        content = content.replace("%7D","}")
        content = content.replace("%20"," ")
        template = Template(content)
        return template.render({'object': data})
