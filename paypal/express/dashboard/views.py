from django.views import generic
from django.conf import settings

from core.utils import cart_to_html, addrs_to_html

from paypal.express import models


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
