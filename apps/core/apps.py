from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.core.signals import request_finished
        from django.db.backends.signals import connection_created
        from django.db.models.signals import post_migrate, pre_migrate

        from . import rls

        # Row-Level Security (ADR-026): variables de sesión y políticas.
        connection_created.connect(rls.on_connection_created, dispatch_uid="core_rls_connection")
        request_finished.connect(rls.on_request_finished, dispatch_uid="core_rls_request_finished")
        pre_migrate.connect(rls.on_pre_migrate, sender=self, dispatch_uid="core_rls_pre_migrate")
        post_migrate.connect(rls.on_post_migrate, sender=self, dispatch_uid="core_rls_post_migrate")
