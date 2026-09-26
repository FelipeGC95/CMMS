"""Los períodos de estado del activo quedan enlazados a la OT y a la falla que los explican (AST-04 CA2)."""

from apps.assets.models import Asset, AssetStatusPeriod
from apps.work.models import FailureEvent
from apps.work.services import complete_work_order, report_failure, transition


def test_stop_repair_and_close_link_the_periods_to_the_work_order_and_failure(in_a):
    request = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="No parte", asset_stopped=True
    )
    wo = request.work_order
    failure = FailureEvent.objects.get(work_order=wo)

    transition(work_order=wo, to_status="in_progress", membership=in_a.tech)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=30)

    periods = {p.status: p for p in AssetStatusPeriod.objects.filter(asset=in_a.asset)}
    stopped = periods[Asset.Status.STOPPED]
    assert (stopped.work_order_id, stopped.failure_event_id) == (wo.pk, failure.pk)
    assert periods[Asset.Status.IN_MAINTENANCE].work_order_id == wo.pk
    operational = periods[Asset.Status.OPERATIONAL]
    assert (operational.work_order_id, operational.failure_event_id) == (wo.pk, failure.pk)
