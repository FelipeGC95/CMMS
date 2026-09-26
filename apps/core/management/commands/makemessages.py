"""`makemessages` con los valores por defecto del proyecto (X-07).

`uv run python manage.py makemessages` regenera `locale/es/LC_MESSAGES/django.po`
con las cadenas de plantillas y código Python. Requiere GNU gettext
(`xgettext`, `msgmerge`): en Ubuntu `apt install gettext`; en Windows el
paquete portable de https://github.com/mlocati/gettext-iconv-windows.

El español es el idioma fuente: los `msgstr` quedan vacíos y Django muestra
el `msgid`, así que no hace falta compilar el `.mo` hasta que existan
traducciones (inglés y portugués, F3).
"""

from django.core.management.commands import makemessages

DEFAULT_LOCALE = "es"
PROJECT_IGNORE = ["tests", "docs", "Marca"]


class Command(makemessages.Command):
    def handle(self, *args, **options):
        if not options["locale"] and not options["all"] and not options["exclude"]:
            options["locale"] = [DEFAULT_LOCALE]
        options["ignore_patterns"] = [*options["ignore_patterns"], *PROJECT_IGNORE]
        # Sin referencias a archivos: las rutas cambian entre Windows y Linux y
        # cada edición de plantilla ensuciaría el diff del catálogo.
        options["no_location"] = True
        options["no_obsolete"] = True
        return super().handle(*args, **options)
