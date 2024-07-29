FROM registry.container-registry/odoo:17.0

COPY --chown=odoo:odoo . /var/lib/odoo/addons/17.0/

EXPOSE 8069 8071 8072

ENTRYPOINT [ "/entrypoint.sh" ]
CMD ["odoo", "-u", "mceasy_erp"]