from django.conf.urls import patterns, url
from django.contrib.admin.views.decorators import staff_member_required

from oscar.core.application import Application

from paypal.express.dashboard import views


class ExpressDashboardApplication(Application):
    name = None
    index_view = views.IndexView
    list_view = views.TransactionListView
    list_wo_order_view = views.TransactionWoOrderListView
    detail_view = views.TransactionDetailView

    def get_urls(self):
        urlpatterns = patterns('',
            url(r'^transactions/$', self.index_view.as_view(),
                name='paypal-express-transaction-index'),
            url(r'^transactions_all/$', self.list_view.as_view(),
                name='paypal-express-transaction-list'),
            url(r'^transactions_wo_order/$',
                self.list_wo_order_view.as_view(),
                name='paypal-express-transaction-wo-order-list'),
            url(r'^transactions/(?P<pk>\d+)/$', self.detail_view.as_view(),
                name='paypal-express-detail'),
        )
        return self.post_process_urls(urlpatterns)

    def get_url_decorator(self, url_name):
        return staff_member_required


application = ExpressDashboardApplication()
