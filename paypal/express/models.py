from __future__ import unicode_literals
import re

from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.contrib.auth import get_user_model
from django.utils.translation import ugettext_lazy as _

from django_extensions.db.fields import CreationDateTimeField

from paypal import base

Basket = models.get_model('basket', 'Basket')


@python_2_unicode_compatible
class ExpressTransaction(base.ResponseModel):

    # The PayPal method and version used
    method = models.CharField(max_length=32)
    version = models.CharField(max_length=8)

    # Transaction details used in GetExpressCheckout
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True,
                                 blank=True)
    currency = models.CharField(max_length=8, null=True, blank=True)

    # Response params
    SUCCESS, SUCCESS_WITH_WARNING, FAILURE = 'Success', 'SuccessWithWarning', 'Failure'
    ack = models.CharField(max_length=32)

    correlation_id = models.CharField(max_length=32, null=True, blank=True)
    token = models.CharField(max_length=32, null=True, blank=True)

    error_code = models.CharField(max_length=32, null=True, blank=True)
    error_message = models.CharField(max_length=256, null=True, blank=True)

    class Meta:
        ordering = ('-date_created',)
        app_label = 'paypal'

    def save(self, *args, **kwargs):
        self.raw_request = re.sub(r'PWD=\d+&', 'PWD=XXXXXX&', self.raw_request)
        return super(ExpressTransaction, self).save(*args, **kwargs)

    @property
    def is_successful(self):
        return self.ack in (self.SUCCESS, self.SUCCESS_WITH_WARNING)

    def __str__(self):
        return 'method: %s: token: %s' % (
            self.method, self.token)


class ExpressTransactionPreAuth(models.Model):
    """
    store pre-auth data
    """
    billing_addr = models.TextField(blank=True, null=True)
    shipping_addr = models.TextField(blank=True, null=True)
    shopping_cart = models.TextField(blank=True, null=True)
    token = models.CharField(_("Token"), max_length=32, null=False,
                             blank=False)
    email = models.CharField(_("E-Mail"), max_length=128, null=True,
                             blank=True)
    customer = models.ForeignKey(get_user_model(), blank=True, null=True,
                                 on_delete=models.SET_NULL,
                                 related_name="express_transaction")
    created = CreationDateTimeField()
    basket = models.ForeignKey(Basket, blank=True, null=True,
                               related_name="express_transaction",
                               on_delete=models.SET_NULL)
