"""Almacenamiento de estáticos en producción (ADR-011).

Igual que el de whitenoise (nombres con hash y compresión), pero además reescribe los
`import`/`export … from` de los módulos ES para que apunten a los archivos con hash:
la vista de terreno (`static/field/*.js`) se carga como módulos sin paso de build y cada
despliegue invalida solo lo que cambió.
"""

from whitenoise.storage import CompressedManifestStaticFilesStorage


class ModuleManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    support_js_module_import_aggregation = True
