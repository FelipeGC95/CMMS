"""Datos de demostración: una pyme de alimentos ficticia y una segunda organización.

Uso: python manage.py seed_demo [--reset | --reset-if-stale]

`--reset-if-stale` (lo usa Render en cada arranque) recrea la demo solo si le faltan datos de
módulos posteriores a su carga (inventario, condición, asistente); si está al día no toca nada.
"""

import datetime
import os
import random
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.accounts.services import create_user
from apps.assets.models import Asset, AssetClass, AssetStatusPeriod, Meter, MeterKind
from apps.assets.services import record_meter_reading
from apps.assistant.models import AssistantConversation
from apps.condition.models import ConditionRule
from apps.core.tenancy import platform_context, tenant_context
from apps.inventory.models import Part
from apps.maintenance.models import ItemType
from apps.maintenance.services import assign_plan, create_plan, generate_due_work_orders
from apps.organizations.models import Location, Organization, Plan
from apps.organizations.plans import ensure_default_plans
from apps.work.models import FailureCode, FailureEvent, LaborRate, WorkOrder
from apps.work.selectors import OPEN_STATUSES
from apps.work.services import answer_checklist_item, complete_work_order, create_work_order

from . import _seed_assistant as demo_assistant
from . import _seed_condition as demo_condition
from . import _seed_inventory as demo_inventory

# En ambientes publicados se define DEMO_PASSWORD (la de por defecto está en el repo).
PASSWORD = os.environ.get("DEMO_PASSWORD") or "demo-galbust-2026"

LOCATIONS = [
    ("PLT", "Planta Osorno", None, Location.Kind.SITE),
    ("ENV", "Línea de envasado", "PLT", Location.Kind.LINE),
    ("CAL", "Sala de calderas", "PLT", Location.Kind.AREA),
    ("FRI", "Cámaras de frío", "PLT", Location.Kind.AREA),
    ("BOD", "Bodega y despacho", "PLT", Location.Kind.AREA),
]

CLASSES = [
    ("GRH", "Grúa horquilla"),
    ("CMP", "Compresor de aire"),
    ("CAL", "Caldera"),
    ("ENV", "Envasadora"),
    ("FRI", "Equipo de frío"),
    ("VEH", "Vehículo liviano"),
    ("GEN", "Grupo electrógeno"),
]

ASSETS = [
    # código, nombre, clase, ubicación, criticidad, fabricante, modelo, año
    ("GRH-01", "Grúa horquilla eléctrica 2,5 t", "GRH", "BOD", "B", "Toyota", "8FBE25", 2019),
    ("GRH-02", "Grúa horquilla GLP 3 t", "GRH", "BOD", "B", "Hyster", "H3.0FT", 2017),
    ("CMP-01", "Compresor de tornillo 30 HP", "CMP", "ENV", "A", "Atlas Copco", "GA22", 2018),
    ("CMP-02", "Compresor de respaldo 15 HP", "CMP", "ENV", "C", "Kaeser", "SM 15", 2014),
    ("CAL-01", "Caldera a gas 1.500 kg/h", "CAL", "CAL", "A", "Loos", "UT-L 12", 2016),
    ("ENV-01", "Envasadora vertical línea 1", "ENV", "ENV", "A", "Ulma", "VTI 250", 2020),
    ("ENV-02", "Envasadora vertical línea 2", "ENV", "ENV", "A", "Ulma", "VTI 250", 2021),
    ("ENV-03", "Selladora de bandejas", "ENV", "ENV", "B", "Multivac", "T 200", 2015),
    ("FRI-01", "Unidad condensadora cámara 1", "FRI", "FRI", "A", "Bitzer", "LH84E", 2018),
    ("FRI-02", "Unidad condensadora cámara 2", "FRI", "FRI", "A", "Bitzer", "LH84E", 2018),
    ("VEH-01", "Camioneta de reparto", "VEH", "BOD", "C", "Toyota", "Hilux 2.4", 2022),
    ("GEN-01", "Grupo electrógeno 200 kVA", "GEN", "PLT", "B", "Cummins", "C200 D5", 2013),
]

PLANS = [
    (
        "PM-GRH-SEM",
        "Inspección semanal grúa horquilla",
        "GRH",
        7,
        30,
        [
            ("Revisar nivel de batería / GLP", ItemType.CHECK),
            ("Probar frenos de servicio y estacionamiento", ItemType.CHECK),
            ("Revisar cadenas y horquillas (fisuras, desgaste)", ItemType.CHECK),
            ("Probar bocina, luces y alarma de retroceso", ItemType.CHECK),
            ("Presión de neumáticos", ItemType.MEASUREMENT, "psi", 90, 110),
        ],
    ),
    (
        "PM-CMP-MEN",
        "Mantención mensual compresor",
        "CMP",
        30,
        90,
        [
            ("Purgar condensado del estanque", ItemType.CHECK),
            ("Limpiar filtro de aspiración", ItemType.CHECK),
            ("Nivel de aceite", ItemType.CHECK),
            ("Temperatura de descarga", ItemType.MEASUREMENT, "°C", 70, 95),
            ("Presión de trabajo", ItemType.MEASUREMENT, "bar", 6.5, 8),
        ],
    ),
    (
        "PM-CAL-TRI",
        "Revisión trimestral caldera",
        "CAL",
        90,
        240,
        [
            ("Probar válvulas de seguridad", ItemType.CHECK),
            ("Revisar quemador y electrodos", ItemType.CHECK),
            ("Purga de fondo", ItemType.CHECK),
            ("Presión de vapor", ItemType.MEASUREMENT, "bar", 6, 9),
        ],
    ),
    (
        "PM-ENV-QUI",
        "Mantención quincenal envasadora",
        "ENV",
        14,
        60,
        [
            ("Limpiar mordazas de sellado", ItemType.CHECK),
            ("Revisar teflón de mordazas", ItemType.CHECK),
            ("Lubricar guías y cadenas", ItemType.CHECK),
            ("Temperatura de sellado vertical", ItemType.MEASUREMENT, "°C", 140, 170),
        ],
    ),
    (
        "PM-FRI-MEN",
        "Mantención mensual equipo de frío",
        "FRI",
        30,
        90,
        [
            ("Limpiar condensador", ItemType.CHECK),
            ("Revisar fugas (visual y detector)", ItemType.CHECK),
            ("Temperatura de cámara", ItemType.MEASUREMENT, "°C", -22, -16),
        ],
    ),
    (
        "PM-GEN-MEN",
        "Prueba mensual grupo electrógeno",
        "GEN",
        30,
        45,
        [
            ("Nivel de combustible y aceite", ItemType.CHECK),
            ("Arranque y marcha en vacío 15 min", ItemType.CHECK),
            ("Voltaje de salida", ItemType.MEASUREMENT, "V", 370, 400),
        ],
    ),
]

# Medidores: activo, tipo, lectura hace 35 días, uso diario promedio.
METERS = [
    ("GRH-01", MeterKind.HOURS, 4200, 6.5),
    ("GRH-02", MeterKind.HOURS, 8730, 7.5),
    ("CMP-01", MeterKind.HOURS, 21000, 16),
    ("CMP-02", MeterKind.HOURS, 9500, 2),
    ("GEN-01", MeterKind.HOURS, 1830, 0.4),
    ("VEH-01", MeterKind.KM, 48500, 140),
]

# Planes por uso (se asignan después del historial): código, nombre, clase,
# medidor, intervalo, días (mixto) o None, minutos, ítems, {activo: faltante}.
METER_PLANS = [
    (
        "PM-GRH-250",
        "Mantención 250 h grúa horquilla",
        "GRH",
        MeterKind.HOURS,
        250,
        None,
        120,
        [
            ("Cambiar aceite de motor / reductor", ItemType.CHECK),
            ("Cambiar filtros de aceite y aire", ItemType.CHECK),
            ("Revisar nivel de aceite hidráulico", ItemType.CHECK),
            ("Engrasar mástil y pivotes", ItemType.CHECK),
            ("Holgura de cadenas de elevación", ItemType.MEASUREMENT, "mm", 0, 3),
        ],
        # Faltante para el vencimiento: GRH-02 ya está por vencer, GRH-01 no.
        {"GRH-01": 170, "GRH-02": 12},
    ),
    (
        # Absorbe a la de 250 h (PM-05 CA3): en GRH-02 vencen juntas y se genera una sola OT.
        "PM-GRH-500",
        "Mantención 500 h grúa horquilla",
        "GRH",
        MeterKind.HOURS,
        500,
        None,
        180,
        [
            ("Cambiar aceite hidráulico y filtro de retorno", ItemType.CHECK),
            ("Ajustar frenos y freno de estacionamiento", ItemType.CHECK),
            ("Estado de horquillas y cadenas (describir)", ItemType.TEXT),
            ("Foto de la placa de capacidad", ItemType.PHOTO),
            ("Firma del supervisor de bodega", ItemType.SIGNATURE),
        ],
        {"GRH-02": 12},
    ),
    (
        "PM-VEH-10K",
        "Mantención 10.000 km camioneta",
        "VEH",
        MeterKind.KM,
        10000,
        180,
        150,
        [
            ("Cambiar aceite y filtro de motor", ItemType.CHECK),
            ("Revisar pastillas y discos de freno", ItemType.CHECK),
            ("Rotar neumáticos", ItemType.CHECK),
            ("Profundidad de dibujo de neumáticos", ItemType.MEASUREMENT, "mm", 1.6, 12),
        ],
        {"VEH-01": 1200},
    ),
]

# Historial simulado: 12 semanas, lo que miden por defecto los indicadores ampliados (KPI-02).
HISTORY_DAYS = 84

# Tarifas de mano de obra (WO-04 CA1): general y una personal; se copian a cada registro de horas.
LABOR_RATE = Decimal("18000")
LABOR_RATE_TECH2 = Decimal("22000")

HISTORY_FAILURES = [
    # activo, hace N días, horas detenido, descripción, modo, causa,
    # repuestos del catálogo [(código, cantidad)] (al costo promedio), otro costo (descripción, monto) o None
    ("FRI-01", 78, 9.0, "Cámara 1 sin frío por falla de contactor", "BRD", "ELE",
     [("REP-FRI-001", 1)], ("Técnico de frío externo", 180000)),
    ("ENV-01", 66, 4.0, "Mordaza no corta el film", "ERO", "DES",
     [("REP-ENV-003", 1)], None),
    ("CMP-01", 57, 6.5, "Fuga de aceite en enfriador del compresor", "ELP", "MAT",
     [("REP-CMP-004", 1), ("REP-CMP-003", 2)], None),
    ("GRH-01", 49, 3.0, "Grúa no levanta carga completa", "ERO", "ELE",
     [("REP-GRH-007", 1)], None),
    ("ENV-02", 41, 2.5, "Mordaza vertical no sella", "ERO", "DES",
     [("REP-ENV-001", 2)], None),
    ("CAL-01", 35, 5.0, "Caldera se apaga por bajo nivel de agua", "BRD", "SUC",
     [("REP-CAL-001", 1)], ("Limpieza química de serpentín", 240000)),
    ("ENV-01", 26, 5.5, "Mordaza vertical no sella, bolsas abiertas", "ERO", "DES",
     [("REP-ENV-001", 2)], None),
    ("CMP-01", 21, 3.0, "Compresor se detiene por alta temperatura", "OHE", "SUC",
     [("REP-CMP-001", 1)], None),
    ("FRI-02", 17, 7.0, "Cámara 2 no baja de -12 °C", "BRD", "ELE",
     [("REP-FRI-002", 1), ("REP-FRI-004", 3)], None),
    ("ENV-02", 12, 2.0, "Film se corta en el formador", "ERO", "OPE", [], None),
    ("GRH-02", 9, 4.5, "Fuga de aceite hidráulico en cilindro de elevación", "ELP", "MAT",
     [("REP-GRH-006", 1), ("REP-GRH-004", 1)], None),
    ("ENV-01", 6, 3.5, "Ruido fuerte en motor de arrastre", "NOI", "LUB",
     [("REP-ENV-004", 2)], None),
    ("CAL-01", 3, 2.5, "Quemador no enciende en la mañana", "BRD", "ELE",
     [("REP-CAL-002", 1)], None),
]  # fmt: skip


class Command(BaseCommand):
    help = "Carga datos de demostración (organizaciones, usuarios, activos, planes, OT)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Vacía TODA la base antes (solo demo).")
        parser.add_argument(
            "--reset-if-stale",
            action="store_true",
            help="Como --reset, pero solo si a la demo le faltan inventario, condición o asistente (Render).",
        )

    def handle(self, *args, **options):
        # Comando de plataforma (ADR-026): RLS desactivado; cada escritura igual usa tenant_context.
        with platform_context():
            self._seed(*args, **options)

    def _seed(self, *args, reset=False, reset_if_stale=False, **options):
        random.seed(42)
        if reset_if_stale and self._is_stale():
            self.stdout.write("La demo no tiene inventario, condición o asistente: se recrea completa.")
            reset = True
        if reset:
            # Base de demo: se vacía completa. (El borrado de una organización real es X-04.)
            call_command("flush", interactive=False, verbosity=0)
        if Organization.objects.filter(slug="alimentos-del-sur").exists():
            # Render corre seed_demo en cada arranque: los permisos del admin se ponen al
            # día aunque los datos ya existan (modelos nuevos como versiones o auditoría).
            if admin := User.objects.filter(email="jefe@demo.cl").first():
                self._grant_admin_permissions(admin)
            demo = Organization.objects.get(slug="alimentos-del-sur")
            self._assign_open_work_orders(demo)
            # Render no tiene disco: los PDF de los manuales demo se pierden al dormir.
            with tenant_context(demo):
                demo_assistant.restore_manual_files()
            # Demo con usuarios compartidos: sin segundo factor obligatorio (ORG-08).
            Organization.objects.filter(slug__in=["alimentos-del-sur", "maestranza-norte"]).update(
                mfa_policy=Organization.MfaPolicy.OPTIONAL
            )
            self.stdout.write("Los datos demo ya existen. Usa --reset para recrearlos.")
            return

        with transaction.atomic():
            ensure_default_plans(Plan)  # `--reset` vacía también el catálogo de planes
            pro = Plan.objects.get(code="pro")
            platform = self._user("plataforma@galbust.cl", "Equipo GALBUST", superuser=True)
            org = Organization.objects.create(
                name="Alimentos del Sur SpA",
                slug="alimentos-del-sur",
                plan=pro,
                # Demo pública con usuarios compartidos: sin segundo factor obligatorio (ORG-08).
                mfa_policy=Organization.MfaPolicy.OPTIONAL,
            )
            admin = self._user("jefe@demo.cl", "Carolina Muñoz", staff=True)
            tech = self._user("tecnico@demo.cl", "Jorge Soto")
            tech2 = self._user("tecnico2@demo.cl", "Marcelo Ríos")
            operator = self._user("operador@demo.cl", "Paula Vera")
            members = {
                "admin": Membership.objects.create(user=admin, organization=org, role="admin"),
                "tech": Membership.objects.create(user=tech, organization=org, role="technician"),
                "tech2": Membership.objects.create(user=tech2, organization=org, role="technician"),
                "operator": Membership.objects.create(user=operator, organization=org, role="viewer"),
            }
            Membership.objects.create(user=platform, organization=org, role="admin")
            with tenant_context(org):
                self._seed_main(org, members)
            self._assign_open_work_orders(org)
            self._schedule_demo_week(org, members["admin"])

            other = Organization.objects.create(
                name="Maestranza Norte Ltda.",
                slug="maestranza-norte",
                plan=Plan.objects.get(code="trial"),
                status=Organization.Status.TRIAL,
                mfa_policy=Organization.MfaPolicy.OPTIONAL,
                trial_ends_at=timezone.now() + datetime.timedelta(days=30),
            )
            Membership.objects.create(user=admin, organization=other, role="viewer")
            with tenant_context(other):
                cls = AssetClass.objects.create(code="TOR", name="Torno CNC")
                Asset.objects.create(
                    code="TOR-01", name="Torno CNC Haas ST-20", asset_class=cls, criticality="A"
                )

        self.stdout.write(self.style.SUCCESS("Datos demo cargados."))
        if not os.environ.get("DEMO_PASSWORD"):
            self.stdout.write(f"Contraseña de todos los usuarios: {PASSWORD}")
        for email, role in [
            ("jefe@demo.cl", "jefe de mantención (admin, 2 organizaciones)"),
            ("tecnico@demo.cl", "técnico"),
            ("operador@demo.cl", "operador (solo avisa fallas)"),
            ("plataforma@galbust.cl", "superadmin de plataforma"),
        ]:
            self.stdout.write(f"  {email:24} {role}")

    def _assign_open_work_orders(self, org):
        """Reparte las OT abiertas sin responsable entre los técnicos demo.

        Sin esto "Mis OT" (vista por defecto del técnico, WO-05) queda vacía en la
        demo. Idempotente: solo toca OT sin responsable, así que también corrige
        la base de Render que ya existe sin recrearla.
        """
        technicians = list(
            User.objects.filter(email__in=["tecnico@demo.cl", "tecnico2@demo.cl"]).order_by("email")
        )
        if not technicians:
            return
        with tenant_context(org):
            pending = WorkOrder.objects.filter(status__in=OPEN_STATUSES, assigned_to__isnull=True).order_by(
                "due_on", "created_at"
            )
            for index, work_order in enumerate(pending):
                work_order.assigned_to = technicians[index % len(technicians)]
                work_order.save(update_fields=["assigned_to", "updated_at"])

    def _schedule_demo_week(self, org, admin):
        """Programa algunas OT en los próximos días hábiles (REQ-05) para que el tablero
        semanal y «Mi semana» no partan vacíos en la demo."""
        from apps.work.services import schedule_work_order

        day = timezone.localdate() + datetime.timedelta(days=1)
        with tenant_context(org):
            planned = WorkOrder.objects.filter(
                status=WorkOrder.Status.PLANNED, assigned_to__isnull=False
            ).order_by("due_on", "created_at")[:4]
            for work_order in planned:
                while day.weekday() >= 5:
                    day += datetime.timedelta(days=1)
                schedule_work_order(work_order=work_order, membership=admin, scheduled_for=day)
                day += datetime.timedelta(days=1)

    def _user(self, email, name, staff=False, superuser=False):
        user = User.objects.filter(email=email).first()
        if user is None:
            # Correo ya verificado: los usuarios demo no reciben el correo de verificación.
            user = create_user(
                email=email,
                password=PASSWORD,
                full_name=name,
                email_verified=True,
                is_staff=staff or superuser,
                is_superuser=superuser,
            )
        if staff and not superuser:
            self._grant_admin_permissions(user)
        return user

    def _grant_admin_permissions(self, user):
        # El admin de la organización configura desde el admin de Django (solo su tenant)
        # y consulta la auditoría, que es de solo lectura.
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label__in=["assets", "maintenance", "work", "organizations"]
            ).exclude(content_type__model__in=["organization", "plan"])
            | Permission.objects.filter(content_type__app_label="audit", codename__startswith="view_")
        )

    @staticmethod
    def _is_stale() -> bool:
        """¿La demo existe pero se cargó antes de inventario, condición o el asistente? (Render.)"""
        org = Organization.objects.filter(slug="alimentos-del-sur").first()
        if org is None:
            return False  # sin demo: se carga normal, no hay nada que vaciar
        with tenant_context(org):
            return not (
                Part.objects.exists()
                and ConditionRule.objects.exists()
                and AssistantConversation.objects.exists()
            )

    def _seed_main(self, org, members):
        now = timezone.now()
        today = timezone.localdate()
        history_start = today - datetime.timedelta(days=HISTORY_DAYS)
        admin, tech = members["admin"], members["tech"]

        locations = {}
        for code, name, parent, kind in LOCATIONS:
            locations[code] = Location.objects.create(
                code=code, name=name, kind=kind, parent=locations.get(parent)
            )
        classes = {code: AssetClass.objects.create(code=code, name=name) for code, name in CLASSES}
        assets = {}
        for code, name, cls, loc, crit, maker, model, year in ASSETS:
            assets[code] = Asset.objects.create(
                code=code,
                name=name,
                asset_class=classes[cls],
                location=locations[loc],
                criticality=crit,
                manufacturer=maker,
                model=model,
                year=year,
                serial_number=f"{maker[:3].upper()}-{random.randint(100000, 999999)}",
            )
            AssetStatusPeriod.objects.create(
                asset=assets[code],
                status="operational",
                started_at=now - datetime.timedelta(days=HISTORY_DAYS + 30),
            )
        # El catálogo base de falla (REQ-03 CA1) ya se cargó al crear `org` (señal en apps.work).
        codes = {c.code: c for c in FailureCode.objects.all()}
        # Inventario (INV): bodega principal + pañol del sitio, catálogo y stock inicial.
        parts = demo_inventory.seed_warehouses_and_parts(
            admin=admin, locations=locations, history_days=HISTORY_DAYS
        )
        self.parts = parts

        # Planes y asignaciones con vencimientos repartidos (algunos atrasados).
        for code, name, cls, interval, minutes, items in PLANS:
            plan = create_plan(
                code=code,
                name=name,
                asset_class=classes[cls],
                calendar_interval=interval,
                estimated_minutes=minutes,
                items=self._items(items),
                resources=demo_inventory.plan_resources(code, parts),
            )
            for asset in (a for a in assets.values() if a.asset_class_id == classes[cls].id):
                first_due = history_start + datetime.timedelta(days=random.randint(0, min(interval, 60) - 1))
                assign_plan(plan=plan, asset=asset, starts_on=first_due)

        # Costos de mano de obra (WO-04): toda hora registrada copia la tarifa vigente.
        LaborRate.objects.create(hourly_cost=LABOR_RATE)
        LaborRate.objects.create(membership=members["tech2"], hourly_cost=LABOR_RATE_TECH2)

        self._seed_meters(assets, now)
        self._simulate_preventive_history(history_start, today, tech)
        self._seed_meter_plans(assets, classes)
        self._seed_pm_engine(assets, classes, today)

        # Fallas históricas reparadas.
        for asset_code, days_ago, hours_down, text, mode, cause, parts, other in HISTORY_FAILURES:
            started = now - datetime.timedelta(days=days_ago, hours=random.randint(1, 8))
            self._historic_failure(
                assets[asset_code],
                started,
                hours_down,
                text,
                codes[mode],
                codes[cause],
                members,
                parts=parts,
                other=other,
            )
        self._cancelled_history(assets, members, now)
        # Mantenimiento por condición (CBM-10): variables, reglas, 90 días de lecturas y episodios.
        demo_condition.seed(org=org, assets=assets, classes=classes, admin=admin, tech=tech, now=now)

        # Situación actual: un equipo crítico detenido y una solicitud nueva.
        from apps.work.services import report_failure, transition

        stopped = report_failure(
            membership=members["operator"],
            asset=assets["FRI-01"],
            description=(
                "Cámara 1 con alarma de alta temperatura (-8 °C)\n"
                "El compresor parte y se detiene a los pocos segundos."
            ),
            asset_stopped=True,
        )
        self._corrective_checklist(stopped.work_order, members)
        # La toma el técnico de la demo: la ve en "Mis OT" y ejecuta el checklist desde el celular.
        transition(work_order=stopped.work_order, to_status="in_progress", membership=members["tech"])
        report_failure(
            membership=members["operator"],
            asset=assets["GRH-01"],
            description="Alarma de retroceso no suena",
            asset_stopped=False,
        )
        create_work_order(
            work_type=WorkOrder.Type.IMPROVEMENT,
            title="Instalar protección en transmisión de envasadora 3",
            asset=assets["ENV-03"],
            priority=WorkOrder.Priority.NORMAL,
            due_on=today + datetime.timedelta(days=10),
            estimated_minutes=180,
            by=admin.user,
        )
        # Trabajo sobre una ubicación, sin activo (WO-01 CA2), con un técnico de apoyo;
        # y un aviso sobre una ubicación (REQ-01 CA1).
        create_work_order(
            work_type=WorkOrder.Type.INSPECTION,
            title="Revisar iluminación y extintores de la sala de calderas",
            location=locations["CAL"],
            priority=WorkOrder.Priority.NORMAL,
            due_on=today + datetime.timedelta(days=3),
            estimated_minutes=60,
            assigned_to=tech.user,
            helpers=[members["tech2"].user],
            by=admin.user,
        )
        report_failure(
            membership=members["operator"],
            location=locations["BOD"],
            description="Gotera en el techo de la bodega\nCae agua cerca de los pallets de despacho.",
        )

        # Preventivos vigentes (próximos 14 días y atrasados): reservan sus repuestos previstos.
        generate_due_work_orders(today=today)
        demo_inventory.finish()
        # Asistente de diagnóstico (D3): manuales y conversaciones ya respondidas, sin la API.
        demo_assistant.seed(
            assets=assets, classes=classes, members=members, now=now, open_work_order=stopped.work_order
        )

    def _corrective_checklist(self, work_order, members):
        """Checklist de la correctiva abierta (MVP-0: aviso por QR → OT → checklist en el celular)."""
        from apps.maintenance.models import ItemType, MaintenancePlan
        from apps.work.services import add_checklist_item, apply_plan_checklist

        plan = MaintenancePlan.objects.get(code="PM-FRI-MEN")
        apply_plan_checklist(
            work_order=work_order, plan_version=plan.current_version, membership=members["admin"]
        )
        add_checklist_item(
            work_order=work_order,
            membership=members["admin"],
            text="Revisar presostato de baja y contactor del compresor",
        )
        add_checklist_item(
            work_order=work_order,
            membership=members["admin"],
            text="Corriente del compresor en marcha",
            item_type=ItemType.MEASUREMENT,
            unit="A",
            min_value=18,
            max_value=24,
        )

    def _seed_meters(self, assets, now):
        """Lecturas semanales de las últimas 5 semanas, con algo de variación."""
        for code, kind, start_value, per_day in METERS:
            meter = Meter.objects.create(asset=assets[code], kind=kind)
            value = Decimal(start_value)
            for weeks_ago in range(5, -1, -1):
                if weeks_ago < 5:
                    value += Decimal(str(round(per_day * 7 * random.uniform(0.8, 1.2), 1)))
                read_at = now - datetime.timedelta(days=weeks_ago * 7, hours=random.randint(1, 6))
                record_meter_reading(meter=meter, value=value, read_at=read_at, generate_work_orders=False)

    def _seed_meter_plans(self, assets, classes):
        for code, name, cls, kind, interval, days, minutes, items, remaining in METER_PLANS:
            plan = create_plan(
                code=code,
                name=name,
                asset_class=classes[cls],
                meter_kind=kind,
                meter_interval=Decimal(interval),
                calendar_interval=days,
                estimated_minutes=minutes,
                items=self._items(items),
                resources=demo_inventory.plan_resources(code, self.parts),
            )
            for asset_code, left in remaining.items():
                meter = assets[asset_code].meters.get(kind=kind)
                assign_plan(
                    plan=plan,
                    asset=assets[asset_code],
                    meter=meter,
                    next_due_meter_value=meter.current_value + left,
                )

    def _seed_pm_engine(self, assets, classes, today):
        """Absorción 500 h ⊃ 250 h y un plan mensual de calendario (PM-02 CA1, PM-05 CA3)."""
        from apps.maintenance.models import CalendarUnit, MaintenancePlan, PlanAbsorption

        PlanAbsorption.objects.create(
            plan=MaintenancePlan.objects.get(code="PM-GRH-500"),
            absorbed_plan=MaintenancePlan.objects.get(code="PM-GRH-250"),
        )
        monthly = create_plan(
            code="PM-CMP-MES",
            name="Inspección mensual compresor",
            asset_class=classes["CMP"],
            calendar_interval=1,
            calendar_unit=CalendarUnit.MONTHS,
            estimated_minutes=30,
            items=self._items(
                [
                    ("Revisar fugas de aire en líneas", ItemType.CHECK),
                    ("Horas de operación desde la última inspección", ItemType.TEXT),
                    ("Foto del manómetro principal", ItemType.PHOTO),
                    # Enlazado a la variable (CBM-06): al completar la OT registra la lectura.
                    (
                        "Temperatura de descarga",
                        ItemType.MEASUREMENT,
                        "°C",
                        70,
                        95,
                        (MeterKind.TEMPERATURE, "Descarga"),
                    ),
                ]
            ),
        )
        for asset in (a for a in assets.values() if a.asset_class_id == classes["CMP"].id):
            assign_plan(plan=monthly, asset=asset, starts_on=today + datetime.timedelta(days=3))

    def _items(self, items):
        """Ítems del checklist en el formato de create_plan.

        (texto, tipo[, unidad, mín, máx[, (tipo de variable, nombre)]]): el último, opcional,
        enlaza el ítem de medición a una variable de condición (CBM-06).
        """
        result = []
        for text, item_type, *rng in items:
            kind, name = rng[3] if len(rng) > 3 else ("", "")
            result.append(
                {
                    "text": text,
                    "item_type": item_type,
                    "unit": rng[0] if rng else "",
                    "min_value": Decimal(str(rng[1])) if rng else None,
                    "max_value": Decimal(str(rng[2])) if rng else None,
                    "meter_kind": kind,
                    "meter_name": name,
                }
            )
        return result

    def _simulate_preventive_history(self, start, today, tech):
        """Recorre los días pasados: genera las OT y cierra la mayoría a tiempo."""
        delays = {}
        day = start
        while day < today:
            generate_due_work_orders(today=day, horizon_days=0)
            due = WorkOrder.objects.filter(
                work_type=WorkOrder.Type.PREVENTIVE, status="planned", due_on__lte=day
            )
            for wo in due:
                delay = delays.setdefault(wo.pk, random.choice([0, 0, 0, 0, 0, 1, 3, 6]))
                if day >= wo.due_on + datetime.timedelta(days=delay):
                    self._run_checklist(wo, tech)
                    # Al cerrar en la web se consumen los repuestos previstos (Q2 de INV).
                    complete_work_order(
                        work_order=wo,
                        membership=tech,
                        labor_minutes=wo.estimated_minutes or 60,
                        parts_used=demo_inventory.planned_usage(wo),
                    )
                    self._backdate(wo, day)
            day += datetime.timedelta(days=1)

    def _run_checklist(self, wo, membership):
        for item in wo.checklist.all():
            if item.item_type == ItemType.MEASUREMENT:
                lo, hi = float(item.min_value), float(item.max_value)
                value = round(random.uniform(lo, hi), 1)
                answer_checklist_item(item=item, membership=membership, value_number=value)
            else:
                answer_checklist_item(item=item, membership=membership, result="ok")

    def _backdate(self, wo, done_on):
        """Mueve las fechas de la OT al pasado (solo datos demo)."""
        moment = timezone.make_aware(datetime.datetime.combine(done_on, datetime.time(11, 0)))
        WorkOrder.objects.filter(pk=wo.pk).update(
            started_at=moment - datetime.timedelta(hours=1),
            completed_at=moment,
            closed_at=moment,
            created_at=moment - datetime.timedelta(days=3),
        )
        wo.transitions.update(created_at=moment)
        demo_inventory.backdate_movements(wo.stock_movements.all(), moment)
        if wo.plan_assignment_id:
            from apps.maintenance.services import register_completion

            wo.plan_assignment.refresh_from_db()
            register_completion(assignment=wo.plan_assignment, completed_on=done_on)

    def _cancelled_history(self, assets, members, now):
        """OT anuladas en el historial: salen del backlog en la fecha de su cancelación (KPI-02)."""
        from apps.work.services import transition

        for asset_code, days_ago, title in [
            ("VEH-01", 30, "Revisar ruido en suspensión (duplicada)"),
            ("GEN-01", 55, "Cambiar baterías de partida (se hizo en la preventiva)"),
        ]:
            wo = create_work_order(
                work_type=WorkOrder.Type.CORRECTIVE,
                title=title,
                asset=assets[asset_code],
                estimated_minutes=120,
                by=members["admin"].user,
            )
            transition(work_order=wo, to_status="cancelled", membership=members["admin"])
            created = now - datetime.timedelta(days=days_ago)
            WorkOrder.objects.filter(pk=wo.pk).update(created_at=created)
            wo.transitions.update(created_at=created + datetime.timedelta(days=4))

    def _historic_failure(self, asset, started, hours_down, text, mode, cause, members, *, parts, other):
        from apps.work.services import add_other_cost, add_part_usage, report_failure

        request = report_failure(
            membership=members["operator"], asset=asset, description=text, asset_stopped=True
        )
        wo = request.work_order
        for code, quantity in parts:
            add_part_usage(
                work_order=wo, membership=members["tech"], part=self.parts[code], quantity=quantity
            )
        if other:
            add_other_cost(work_order=wo, membership=members["admin"], description=other[0], amount=other[1])
        complete_work_order(
            work_order=wo,
            membership=members["tech"],
            labor_minutes=int(hours_down * 60 * 0.8),
            notes="Reparado y probado en operación.",
            failure_mode=mode,
            failure_cause=cause,
        )
        ended = started + datetime.timedelta(hours=hours_down)
        FailureEvent.objects.filter(work_order=wo).update(started_at=started, ended_at=ended)
        periods = list(AssetStatusPeriod.objects.filter(asset=asset).order_by("-started_at")[:3])
        # Últimos 3 períodos: detenido → en mantención → operativo; se ajustan a la fecha real.
        operational, maintenance, stopped = periods
        repair_start = started + datetime.timedelta(hours=hours_down * 0.3)
        AssetStatusPeriod.objects.filter(pk=stopped.pk).update(started_at=started, ended_at=repair_start)
        AssetStatusPeriod.objects.filter(pk=maintenance.pk).update(started_at=repair_start, ended_at=ended)
        AssetStatusPeriod.objects.filter(pk=operational.pk).update(started_at=ended)
        prev = (
            AssetStatusPeriod.objects.filter(asset=asset, ended_at__isnull=False)
            .exclude(pk__in=[stopped.pk, maintenance.pk])
            .order_by("-started_at")
            .first()
        )
        if prev:
            AssetStatusPeriod.objects.filter(pk=prev.pk).update(ended_at=started)
        WorkOrder.objects.filter(pk=wo.pk).update(
            created_at=started, started_at=repair_start, completed_at=ended, closed_at=ended
        )
        wo.transitions.update(created_at=ended)
        demo_inventory.backdate_movements(wo.stock_movements.all(), repair_start)
        request.__class__.objects.filter(pk=request.pk).update(created_at=started)
