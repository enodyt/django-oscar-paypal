from __future__ import unicode_literals
from decimal import Decimal as D
import logging

from django.views.generic import RedirectView, View
from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.utils.http import urlencode
from django.utils import six
from django.utils.translation import ugettext_lazy as _

import oscar
from oscar.apps.payment.exceptions import RedirectRequired
from oscar.core.loading import get_class, get_model
from oscar.apps.shipping.methods import FixedPrice, NoShippingRequired
from oscar.apps.checkout import signals

from paypal.express.facade import (
    get_paypal_url, fetch_transaction_details, confirm_transaction)
from paypal.express.exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, InvalidBasket)
from paypal.exceptions import PayPalError

# Load views dynamically
PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')

ShippingAddress = get_model('order', 'ShippingAddress')
Country = get_model('address', 'Country')
Basket = get_model('basket', 'Basket')
Repository = get_class('shipping.repository', 'Repository')
Applicator = get_class('offer.utils', 'Applicator')
Selector = get_class('partner.strategy', 'Selector')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
Order = get_model("order", "Order")
OrderPlacementMixin = get_class('checkout.mixins', 'OrderPlacementMixin')

logger = logging.getLogger('paypal.express')


class RedirectView(CheckoutSessionMixin, RedirectView):
    """
    Initiate the transaction with Paypal and redirect the user
    to PayPal's Express Checkout to perform the transaction.
    """
    permanent = False

    # Setting to distinguish if the site has already collected a shipping
    # address.  This is False when redirecting to PayPal straight from the
    # basket page but True when redirecting from checkout.
    as_payment_method = False

    def get_redirect_url(self, **kwargs):
        try:
            basket = self.request.basket
            url = self._get_redirect_url(basket, **kwargs)
        except PayPalError:
            messages.error(
                self.request, _("An error occurred communicating with PayPal"))
            if self.as_payment_method:
                url = reverse('basket:summary')
            else:
                url = reverse('basket:summary')
            return url
        except InvalidBasket as e:
            messages.warning(self.request, six.text_type(e))
            return reverse('basket:summary')
        except EmptyBasketException:
            messages.error(self.request, _("Your basket is empty"))
            return reverse('basket:summary')
        except MissingShippingAddressException:
            messages.error(
                self.request, _("A shipping address must be specified"))
            return reverse('checkout:shipping-address')
        except MissingShippingMethodException:
            messages.error(
                self.request, _("A shipping method must be specified"))
            return reverse('checkout:shipping-method')
        else:
            # Transaction successfully registered with PayPal.  Now freeze the
            # basket so it can't be edited while the customer is on the PayPal
            # site.
            basket.freeze()

            logger.info("Basket #%s - redirecting to %s", basket.id, url)

            return url

    def _get_redirect_url(self, basket, **kwargs):
        if basket.is_empty:
            raise EmptyBasketException()

        params = {
            'basket': basket,
            'shipping_methods': []          # setup a default empty list
        }                                   # to support no_shipping

        user = self.request.user
        if self.as_payment_method:
            if basket.is_shipping_required():
                # Only check for shipping details if required.
                shipping_addr = self.get_shipping_address(basket)
                if not shipping_addr:
                    raise MissingShippingAddressException()

                shipping_method = self.get_shipping_method(
                    basket, shipping_addr)
                if not shipping_method:
                    raise MissingShippingMethodException()

                params['shipping_address'] = shipping_addr
                params['shipping_method'] = shipping_method
                params['shipping_methods'] = []

        else:
            shipping_methods = Repository().get_shipping_methods(
                user=user, basket=basket)
            params['shipping_methods'] = shipping_methods

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        if user.is_authenticated():
            params['user'] = user

        params['paypal_params'] = self._get_paypal_params()

        return get_paypal_url(**params)

    def _get_paypal_params(self):
        """
        Return any additional PayPal parameters
        """
        return {}


class CancelResponseView(RedirectView):
    permanent = False

    def get(self, request, *args, **kwargs):
        try:
            # handle order already generated (in case of errors: 10486)
            order = Order.objects.get(basket_id=kwargs["basket_id"])
            # change order state
            order.set_status("Cancelled")
            order.save()
            # set redirect url -> order and not basket
            self._redirect_url = reverse('customer:order', kwargs={
                "order_number": order.number})
        except Order.DoesNotExist:
            basket = get_object_or_404(Basket, id=kwargs['basket_id'],
                                       status=Basket.FROZEN)
            basket.thaw()
            logger.info("Payment cancelled (token %s) - basket #%s thawed",
                        request.GET.get('token', '<no token>'), basket.id)
        return super(CancelResponseView, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        messages.error(self.request, _("PayPal transaction cancelled"))
        try:
            return self._redirect_url
        except AttributeError:
            return reverse('basket:summary')


class HandlePaymentView(OrderPlacementMixin, RedirectView):

    def get(self, request, *args, **kwargs):
        """
        Complete payment with PayPal - this calls the 'DoExpressCheckout'
        method to capture the money from the initial transaction.
        """
        def handle_paypal_error(order_number, amount, correlation_id,
                                code=None):
            error_msg = _("A problem occurred while processing payment for "
                          "this order - no payment has been taken.  Please "
                          "contact customer services if this problem persists")
            if code:
                error_msg += ' [Code: %s]' % code
            messages.error(self.request, error_msg)
            # set order status
            order = Order.objects.get(number=order_number)
            order.set_status("Cancelled")
            order.save()
            self.add_payment_event('Failure', amount, reference=correlation_id)
            # set redirect url
            self._redirect_url = reverse('customer:order', kwargs={
                "order_number": order.number})

        order = Order.objects.get(number=kwargs["order_number"])
        order_number = kwargs["order_number"]
        currency = kwargs["currency"]
        amount = kwargs["amount"]
        token = kwargs["token"]
        payer_id = kwargs["payer_id"]

        # add payment source
        source_type, is_created = SourceType.objects.get_or_create(
            name='PayPal')
        source = Source(source_type=source_type,
                        currency=currency,
                        amount_allocated=amount,
                        amount_debited=amount)
        self.add_payment_source(source)

        try:
            confirm_txn = confirm_transaction(
                payer_id, token, amount, currency)
        except PayPalError as e:
            # 10486 error should be redirect to paypal
            if e.message['code'] == '10486':
                if getattr(settings, 'PAYPAL_SANDBOX_MODE', True):
                    url = 'https://www.sandbox.paypal.com/webscr'
                else:
                    url = 'https://www.paypal.com/webscr'
                params = (('cmd', '_express-checkout'),
                          ('token', token),)
                url = '%s?%s' % (url, urlencode(params))
                # we need to redirect to paypal so do so
                self._redirect_url = url
            else:
                handle_paypal_error(order_number,
                                    amount,
                                    e.message["correlation_id"],
                                    code=e.message["code"])
        else:
            if not confirm_txn.is_successful:
                # irgend ein anderer Grund wieso es nicht geklappt hat
                handle_paypal_error(
                    order_number, amount, confirm_txn.correlation_id)
            else:
                # everythings seems ok: Record payment source and event
                self.add_payment_event('Settled', confirm_txn.amount,
                                       reference=confirm_txn.correlation_id)

        # finally (and in any case) save payment detail
        self.save_payment_details(order)
        return super(HandlePaymentView, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        try:
            return self._redirect_url
        except AttributeError:
            return reverse("checkout:thank-you")


# Upgrading notes: when we drop support for Oscar 0.6, this class can be
# refactored to pass variables around more explicitly (instead of assigning
# things to self so they are accessible in a later method).
class SuccessResponseView(PaymentDetailsView):
    template_name_preview = 'paypal/express/preview.html'
    preview = True

    # We don't have the usual pre-conditions (Oscar 0.7+)
    @property
    def pre_conditions(self):
        return [] if oscar.VERSION[:2] >= (0, 8) else ()

    def get(self, request, *args, **kwargs):
        """
        Fetch details about the successful transaction from PayPal.  We use
        these details to show a preview of the order with a 'submit' button to
        place it.
        """
        # wir pruefen ob schon eine Order mit der basket_id gibt, wenn ja
        # ist dies ein Hinweis auf einen PayPal Error (10486)
        try:
            order = Order.objects.get(basket_id=kwargs["basket_id"])
        except Order.DoesNotExist:
            order = None
        try:
            self.payer_id = request.GET['PayerID']
            self.token = request.GET['token']
        except KeyError:
            # Manipulation - redirect to basket page with warning message
            logger.warning("Missing GET params on success response page")
            messages.error(
                self.request,
                _("Unable to determine PayPal transaction details"))
            return HttpResponseRedirect(reverse('basket:summary'))

        try:
            self.txn = fetch_transaction_details(self.token)
        except PayPalError as e:
            logger.warning(
                "Unable to fetch transaction details for token %s: %s",
                self.token, e)
            messages.error(
                self.request,
                _("A problem occurred communicating with PayPal - please try again later"))

            if not order:
                # keine Order -> Basket
                return HttpResponseRedirect(reverse('basket:summary'))
            else:
                # order -> Order
                return HttpResponseRedirect(reverse('customer:order', kwargs={
                    "order_number": order.number}))

        if order:
            # redirect to handle_paypal_payment
            return HttpResponseRedirect(
                reverse('paypal-handle-payment', kwargs={
                    "order_number": order.number,
                    "payer_id": self.payer_id,
                    "token": self.token,
                    "amount": self.txn.amount,
                    "currency": self.txn.currency,
                }))

        # Reload frozen basket which is specified in the URL
        kwargs['basket'] = self.load_frozen_basket(kwargs['basket_id'])
        if not kwargs['basket']:
            logger.warning(
                "Unable to load frozen basket with ID %s", kwargs['basket_id'])
            messages.error(
                self.request,
                _("No basket was found that corresponds to your "
                  "PayPal transaction"))
            return HttpResponseRedirect(reverse('basket:summary'))

        logger.info(
            "Basket #%s - showing preview with payer ID %s and token %s",
            kwargs['basket'].id, self.payer_id, self.token)

        return super(SuccessResponseView, self).get(request, *args, **kwargs)

    def load_frozen_basket(self, basket_id):
        # Lookup the frozen basket that this txn corresponds to
        try:
            basket = Basket.objects.get(id=basket_id, status=Basket.FROZEN)
        except Basket.DoesNotExist:
            return None

        # Assign strategy to basket instance
        if Selector:
            basket.strategy = Selector().strategy(self.request)

        # Re-apply any offers
        Applicator().apply(self.request, basket)

        return basket

    def get_context_data(self, **kwargs):
        ctx = super(SuccessResponseView, self).get_context_data(**kwargs)

        if not hasattr(self, 'payer_id'):
            return ctx

        # This context generation only runs when in preview mode
        ctx.update({
            'agb_url': settings.AGB_URL,
            'payer_id': self.payer_id,
            'token': self.token,
            'paypal_user_email': self.txn.value('EMAIL'),
            'paypal_amount': D(self.txn.value('AMT')),
        })

        return ctx

    def post(self, request, *args, **kwargs):
        """
        Place an order.

        We fetch the txn details again and then proceed with oscar's standard
        payment details view for placing the order.
        """
        error_msg = _(
            "A problem occurred communicating with PayPal "
            "- please try again later"
        )
        try:
            self.payer_id = request.POST['payer_id']
            self.token = request.POST['token']
        except KeyError:
            # Probably suspicious manipulation if we get here
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))

        try:
            self.txn = fetch_transaction_details(self.token)
        except PayPalError:
            # Unable to fetch txn details from PayPal - we have to bail out
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))

        # Reload frozen basket which is specified in the URL
        basket = self.load_frozen_basket(kwargs['basket_id'])
        if not basket:
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('basket:summary'))

        # submission = self.build_submission(basket=basket)
        # return self.submit(**submission)
        return self.handle_place_order_submission(request, basket=basket)

    def handle_place_order_submission(self, request, basket):
        """
        Handle a request to place an order.

        This method is normally called after the customer has clicked "place
        order" on the preview page. It's responsible for (re-)validating any
        form information then building the submission dict to pass to the
        `submit` method.

        If forms are submitted on your payment details view, you should
        override this method to ensure they are valid before extracting their
        data into the submission dict and passing it onto `submit`.
        """
        url = "%s?token=%s&PayerID=%s" % (
            reverse('paypal-success-response',
                    kwargs=dict(basket_id=basket.id)), self.token,
            self.payer_id)
        # gleiches Land bei Shipping und billing
        shipping_addr = self.get_shipping_address(basket)
        billing_addr = self.get_billing_address(shipping_addr)
        if shipping_addr.country != billing_addr.country:
            error_msg = _("Different Shipping and billing country")
            messages.error(request, error_msg)
            return HttpResponseRedirect(url)
        agbs = request.POST.get("agb", None)
        if not agbs:
            error_msg = _("To place your order, you need "
                          "to agree to our terms and condtition")
            messages.error(request, error_msg)
            return HttpResponseRedirect(url)
        return self.submit(**self.build_submission(basket=basket))

    def build_submission(self, **kwargs):
        submission = super(
            SuccessResponseView, self).build_submission(**kwargs)
        # Pass the user email so it can be stored with the order
        submission['order_kwargs']['guest_email'] = self.txn.value('EMAIL')
        # Pass PP params
        submission['payment_kwargs']['payer_id'] = self.payer_id
        submission['payment_kwargs']['token'] = self.token
        submission['payment_kwargs']['txn'] = self.txn
        return submission

    # Warning: This method can be removed when we drop support for Oscar 0.6
    def get_error_response(self):
        # We bypass the normal session checks for shipping address and shipping
        # method as they don't apply here.
        pass

    def get_success_url(self):
        """
        wir leiten nicht zum thank-you sondern zu unserem handle_payment
        RedirectView:
            1. dort wird dann nochmals mit PayPal (DoExpressCheckout)
               kommuniziert
            2. nach checkout:thank-you redirected
        """
        return reverse('paypal-handle-payment', kwargs={
            "order_number": self.order_number,
            "payer_id": self.payer_id,
            "token": self.token,
            "amount": self.txn.amount,
            "currency": self.txn.currency,
        })

    def handle_payment(self, order_number, total, **kwargs):
        """
        findet in eigener url: handle-payment statt (da wir zuerst eine
        Order erzeugt haben (Transaction erst am Ende des Response) und
        danach erst mit Paypal kommunizieren
        """
        # wir setzen lediglich die order_number um in `get_success_url`
        # darauf zugreifen zu koennen
        self.order_number = order_number
        self.payer_id = kwargs["payer_id"]
        self.token = kwargs["token"]
        self.txn = kwargs["txn"]

    #def get_shipping_address(self, basket):
    #    """
    #    Return a created shipping address instance, created using
    #    the data returned by PayPal.
    #    """
    #    # Determine names - PayPal uses a single field
    #    ship_to_name = self.txn.value('PAYMENTREQUEST_0_SHIPTONAME')
    #    if ship_to_name is None:
    #        return None
    #    first_name = last_name = None
    #    parts = ship_to_name.split()
    #    if len(parts) == 1:
    #        last_name = ship_to_name
    #        first_name = ship_to_name
    #    elif len(parts) > 1:
    #        first_name = parts[0]
    #        last_name = " ".join(parts[1:])
    #    # dodlsicherung
    #    if not first_name:
    #        first_name = ship_to_name
    #    if not last_name:
    #        last_name = ship_to_name
    #    if not first_name:
    #        first_name = 'xxxxxxxxxxxxxx'
    #    if not last_name:
    #        last_name = 'xxxxxxxxxxxxxx'
    #    return ShippingAddress(
    #        first_name=first_name,
    #        last_name=last_name,
    #        line1=self.txn.value('PAYMENTREQUEST_0_SHIPTOSTREET'),
    #        line2=self.txn.value('PAYMENTREQUEST_0_SHIPTOSTREET2', default=""),
    #        line4=self.txn.value('PAYMENTREQUEST_0_SHIPTOCITY', default=""),
    #        state=self.txn.value('PAYMENTREQUEST_0_SHIPTOSTATE', default=""),
    #        postcode=self.txn.value('PAYMENTREQUEST_0_SHIPTOZIP'),
    #        country=Country.objects.get(iso_3166_1_a2=self.txn.value('PAYMENTREQUEST_0_SHIPTOCOUNTRYCODE'))
    #    )

    def get_shipping_method(self, basket, shipping_address=None, **kwargs):
        """
        Return the shipping method used
        """
        if not basket.is_shipping_required():
            return NoShippingRequired()

        # Instantiate a new FixedPrice shipping method instance
        charge_incl_tax = D(self.txn.value('PAYMENTREQUEST_0_SHIPPINGAMT'))

        # Assume no tax for now
        charge_excl_tax = charge_incl_tax
        method = FixedPrice(charge_excl_tax, charge_incl_tax)
        name = self.txn.value('SHIPPINGOPTIONNAME')

        if not name:
            session_method = super(SuccessResponseView, self).get_shipping_method(
                basket, shipping_address, **kwargs)
            if session_method:
                method.name = session_method.name
        else:
            method.name = name
        return method


class ShippingOptionsView(View):

    def post(self, request, *args, **kwargs):
        """
        We use the shipping address given to use by PayPal to
        determine the available shipping method
        """
        # Basket ID is passed within the URL path.  We need to do this as some
        # shipping options depend on the user and basket contents.  PayPal do
        # pass back details of the basket contents but it would be royal pain to
        # reconstitute the basket based on those - easier to just to piggy-back
        # the basket ID in the callback URL.
        basket = get_object_or_404(Basket, id=kwargs['basket_id'])
        user = basket.owner
        if not user:
            user = AnonymousUser()

        # Create a shipping address instance using the data passed back
        country_code = self.request.POST.get(
            'PAYMENTREQUEST_0_SHIPTOCOUNTRY', None)
        try:
            country = Country.objects.get(iso_3166_1_a2=country_code)
        except Country.DoesNotExist:
            country = Country()

        shipping_address = ShippingAddress(
            line1=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOSTREET', None),
            line2=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOSTREET2', None),
            line4=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOCITY', None),
            state=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOSTATE', None),
            postcode=self.request.POST.get('PAYMENTREQUEST_0_SHIPTOZIP', None),
            country=country
        )
        methods = self.get_shipping_methods(user, basket, shipping_address)
        return self.render_to_response(methods, basket)

    def render_to_response(self, methods, basket):
        pairs = [
            ('METHOD', 'CallbackResponse'),
            ('CURRENCYCODE', self.request.POST.get('CURRENCYCODE', 'GBP')),
        ]
        for index, method in enumerate(methods):
            if hasattr(method, 'set_basket'):
                # Oscar < 0.8
                method.set_basket(basket)
                charge = method.charge_incl_tax
            else:
                cost = method.calculate(basket)
                charge = cost.incl_tax

            pairs.append(('L_SHIPPINGOPTIONNAME%d' % index,
                          six.text_type(method.name)))
            pairs.append(('L_SHIPPINGOPTIONLABEL%d' % index,
                          six.text_type(method.name)))
            pairs.append(('L_SHIPPINGOPTIONAMOUNT%d' % index, charge))
            # For now, we assume tax and insurance to be zero
            pairs.append(('L_TAXAMT%d' % index, D('0.00')))
            pairs.append(('L_INSURANCEAMT%d' % index, D('0.00')))
            # We assume that the first returned method is the default one
            pairs.append(('L_SHIPPINGOPTIONISDEFAULT%d' % index, 1 if index == 0 else 0))
        else:
            # No shipping methods available - we flag this up to PayPal indicating that we
            # do not ship to the shipping address.
            pairs.append(('NO_SHIPPING_OPTION_DETAILS', 1))
        payload = urlencode(pairs)
        return HttpResponse(payload)

    def get_shipping_methods(self, user, basket, shipping_address):
        repo = Repository()
        return repo.get_shipping_methods(
            user, basket, shipping_addr=shipping_address)
