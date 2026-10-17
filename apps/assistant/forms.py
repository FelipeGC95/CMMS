"""Formularios del asistente de diagnóstico (AI-01 CA3)."""

import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.work.forms import DICTATION_ATTRS

from .models import AssistantConversation
from .services import MAX_QUESTION_CHARS


class QuestionForm(forms.Form):
    """Una pregunta: texto (con el dictado del teclado) y, si se quiere, una foto de la cámara."""

    text = forms.CharField(
        label=_("¿Qué pasa con el equipo?"),
        help_text=_("Describe el síntoma con tus palabras. Puedes dictarlo con el micrófono del teclado."),
        required=False,
        max_length=MAX_QUESTION_CHARS,
        widget=forms.Textarea(
            attrs={**DICTATION_ATTRS, "placeholder": _("Ej: el motor hace ruido al partir y se calienta")}
        ),
    )
    photo = forms.FileField(
        label=_("Foto (opcional)"),
        help_text=_("Placa, alarma o pieza dañada: una por pregunta."),
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
    )
    # Generado al mostrar el formulario: un doble envío no crea dos preguntas.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)

    def clean(self):
        data = super().clean()
        if not (data.get("text") or "").strip() and not data.get("photo") and not self.errors:
            raise forms.ValidationError(_("Escribe tu pregunta o agrega una foto."))
        return data


class FollowUpForm(QuestionForm):
    """Pregunta siguiente en una conversación abierta."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["text"].label = _("Tu pregunta")
        self.fields["text"].help_text = _("Cuéntale qué mediste o revisaste. Puedes dictarlo.")
        self.fields["text"].widget.attrs["placeholder"] = _("Ej: revisé el rodamiento y está bien")


HELPFUL_CHOICES = AssistantConversation.Helpful.choices
