from odoo import models, fields


def get_default_context(model: models, override_field: dict):
    ignored_fields = ['id', 'display_name', 'create_uid', 'write_uid', 'write_date']
    inherited_field_ids = model.env['ir.model.fields'].search([
        ('model', 'in', model._inherit)
    ])
    inherited_fields = []
    for field_id in inherited_field_ids:
        inherited_fields.append(field_id.name)

    context = {}
    filtered_fields = []
    for field in model._fields:
        if field in inherited_fields or field in ignored_fields:
            continue
        filtered_fields.append(field)

    copied_fields = model.env['ir.model.fields'].search([
        ('model', '=', model._name),
        ('name', 'in', filtered_fields),
        ('copied', '=', True)
    ])
    for field in copied_fields:
        field = field.name
        new_field = f'default_{field}'
        context[new_field] = model[field] if field not in override_field else override_field[field]
        if hasattr(context[new_field], 'id'):
            context[new_field] = context[new_field].id
    return context


def domain_quotation_pricelist_by_uom_category(ctx):
    return ctx.env['uom.category'].search([
        ('for_sale_quotation', '=', True)
    ])
