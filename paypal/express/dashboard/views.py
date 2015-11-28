# -*- coding: utf-8 -*-

from django.views import generic
from django.conf import settings

from oscar.core.loading import get_model

from core.utils import cart_to_html, addrs_to_html

from paypal.express import models

# PaymentSourceType = get_model("payment", "SourceType")
# PaymentSource = get_model("payment", "Source")
PaymentEventType = get_model("order", "PaymentEventType")


class IndexView(generic.TemplateView):
    template_name = 'paypal/express/dashboard/index.html'


class TransactionListView(generic.ListView):
    model = models.ExpressTransaction
    template_name = 'paypal/express/dashboard/transaction_list.html'
    context_object_name = 'transactions'
    paginate_by = 50

    def get_context_data(self, **kwargs):
        # verknuepfe mit preauth data
        ctx = super(TransactionListView, self).get_context_data(**kwargs)
        emails = {}
        for pa in models.ExpressTransactionPreAuth.objects.all():
            emails[pa.token] = pa.email if pa.email else pa.customer.email
        ctx['emails'] = emails
        return ctx


class TransactionWoOrderListView(TransactionListView):
    def get_context_data(self, **kwargs):
        ctx = super(
            TransactionWoOrderListView, self).get_context_data(**kwargs)
        ctx["title"] = (u"Transaktionen ohne Bestellung "
                        u"(enthalten auch abgebrochene Transaktionen)")
        return ctx

    def get_queryset(self):
        # normalerweise muessten wir folgendermassen vorgen:
        # 1. aus PaymentSourceType den paypal source_type_id rausholen
        # 2. PaymentSource nach source_type_id filtern
        # 3. dann daraus alle verknüpften Orders laden
        # 4. daraus wiederum die payment_events rausholen die dann auch
        #    wirklich unseren heissgeliebte correlation_id (=reference)
        #    gespeichert haben.
        #
        # Uff.  Wir kürzen ab, was aber evtl. in Zukunft Probleme bereiten
        # könnte ... und zwar folgendermassen:
        #
        # Wir wissen, dass PayPal als einziges PaymentModul eine erfolgreiche
        # Aktion mit `Settled' kennzeichnet.  Deshalb filtern wir alle
        # PaymentEvents auf dessen ID (bzw. gehen dies im Code mit Back-
        # reference von der umgekehrten Seite an) ...
        pet = PaymentEventType.objects.get(code="settled")
        wo_orders = [p.reference for p in pet.paymentevent_set.all()]
        q = models.ExpressTransaction.objects.exclude(
           correlation_id__in=wo_orders)

        # Da kommen jetzt natürlich verdammt viele Ergebnisse rein, die
        # könnten wir verringern indem wir einfach alle mit SetExpressCheckout
        # (=init) ausklammern
        q = q.exclude(method='SetExpressCheckout')
        return q.order_by("-date_created")


class TransactionDetailView(generic.DetailView):
    model = models.ExpressTransaction
    template_name = 'paypal/express/dashboard/transaction_detail.html'
    context_object_name = 'txn'

    def get_context_data(self, **kwargs):
        ctx = super(TransactionDetailView, self).get_context_data(**kwargs)
        ctx['show_form_buttons'] = getattr(
            settings, 'PAYPAL_PAYFLOW_DASHBOARD_FORMS', False)
        # load additional pre-auth data (if given)
        try:
            pre_auth = models.ExpressTransactionPreAuth.objects.get(
                token=self.object.token)
            ctx["billing_addr"] = addrs_to_html(pre_auth.billing_addr)
            ctx["shipping_addr"] = addrs_to_html(pre_auth.shipping_addr)
            ctx["shopping_cart"] = cart_to_html(pre_auth.shopping_cart)
            ctx["email"] = pre_auth.email if pre_auth.email else pre_auth.customer.email
            ctx["customer"] = pre_auth.customer
            ctx["basket"] = pre_auth.basket
        except models.ExpressTransactionPreAuth.DoesNotExist:
            pass
        return ctx
