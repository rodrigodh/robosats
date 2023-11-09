import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from decouple import config
from django.contrib.auth.models import User
from django.test import Client, TestCase

from api.management.commands.follow_invoices import Command as FollowInvoices
from api.models import Currency, Order
from api.tasks import cache_market
from tests.mocks.cln import MockHoldStub  # , MockNodeStub
from tests.mocks.lnd import (  # MockRouterStub,; MockSignerStub,; MockVersionerStub,
    MockInvoicesStub,
    MockLightningStub,
)


class TradeTest(TestCase):
    su_pass = "12345678"
    su_name = config("ESCROW_USERNAME", cast=str, default="admin")

    def setUp(self):
        """
        Create a superuser. The superuser is the escrow party.
        """
        self.client = Client()
        User.objects.create_superuser(self.su_name, "super@user.com", self.su_pass)

    def test_login_superuser(self):
        """
        Test logging in as a superuser.
        """
        path = "/coordinator/login/"
        data = {"username": self.su_name, "password": self.su_pass}
        response = self.client.post(path, data)
        self.assertEqual(response.status_code, 302)

    def get_robot_auth(self, robot_index, first_encounter=False):
        """
        Create an AUTH header that embeds token, pub_key, and enc_priv_key into a single string
        as requested by the robosats token middleware.
        """
        with open(f"tests/robots/{robot_index}/b91_token", "r") as file:
            b91_token = file.read()
        with open(f"tests/robots/{robot_index}/pub_key", "r") as file:
            pub_key = file.read()
        with open(f"tests/robots/{robot_index}/enc_priv_key", "r") as file:
            enc_priv_key = file.read()

        # First time a robot authenticated, it is registered by the backend, so pub_key and enc_priv_key is needed
        if first_encounter:
            headers = {
                "HTTP_AUTHORIZATION": f"Token {b91_token} | Public {pub_key} | Private {enc_priv_key}"
            }
        else:
            headers = {"HTTP_AUTHORIZATION": f"Token {b91_token}"}

        return headers, pub_key, enc_priv_key

    def assert_robot(self, response, pub_key, enc_priv_key, expected_nickname):
        data = json.loads(response.content.decode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            data["nickname"],
            expected_nickname,
            "Robot created nickname is not MyopicRacket333",
        )
        self.assertEqual(
            data["public_key"], pub_key, "Returned public Kky does not match"
        )
        self.assertEqual(
            data["encrypted_private_key"],
            enc_priv_key,
            "Returned encrypted private key does not match",
        )
        self.assertEqual(
            len(data["tg_token"]), 15, "String is not exactly 15 characters long"
        )
        self.assertEqual(
            data["tg_bot_name"],
            config(
                "TELEGRAM_BOT_NAME", cast=str, default="RoboCoordinatorNotificationBot"
            ),
            "Telegram bot name is not correct",
        )
        self.assertFalse(
            data["tg_enabled"], "The new robot's telegram seems to be enabled"
        )
        self.assertEqual(data["earned_rewards"], 0, "The new robot's rewards are not 0")

    def create_robot(self, robot_index):
        """
        Creates the robots in /tests/robots/{robot_index}
        """
        path = "/api/robot/"
        headers, pub_key, enc_priv_key = self.get_robot_auth(robot_index, True)

        response = self.client.get(path, **headers)

        with open(f"tests/robots/{robot_index}/nickname", "r") as file:
            expected_nickname = file.read()

        self.assert_robot(response, pub_key, enc_priv_key, expected_nickname)

    def test_create_robots(self):
        """
        Creates two robots to be used in the trade tests
        """
        self.create_robot(robot_index=1)
        self.create_robot(robot_index=2)

    def test_cache_market(self):
        cache_market()

        usd = Currency.objects.get(id=1)
        self.assertIsInstance(
            usd.exchange_rate,
            Decimal,
            f"Exchange rate is not a Decimal. Got {type(usd.exchange_rate)}",
        )
        self.assertGreater(
            usd.exchange_rate, 0, "Exchange rate is not higher than zero"
        )
        self.assertIsInstance(
            usd.timestamp, datetime, "External price timestamp is not a datetime"
        )

    def create_order(self, maker_form, robot_index=1):
        # Requisites
        # Cache market prices
        self.test_cache_market()
        path = "/api/make/"
        # Get valid robot auth headers
        headers, _, _ = self.get_robot_auth(robot_index, True)

        response = self.client.post(path, maker_form, **headers)
        return response

    def test_create_order(self):
        maker_form = {
            "type": Order.Types.BUY,
            "currency": 1,
            "has_range": True,
            "min_amount": 21,
            "max_amount": 101.7,
            "payment_method": "Advcash Cash F2F",
            "is_explicit": False,
            "premium": 3.34,
            "public_duration": 69360,
            "escrow_duration": 8700,
            "bond_size": 3.5,
            "latitude": 34.7455,
            "longitude": 135.503,
        }
        response = self.create_order(maker_form, robot_index=1)
        data = json.loads(response.content.decode())

        # Checks
        self.assertIsInstance(data["id"], int, "Order ID is not an integer")
        self.assertEqual(
            data["status"],
            Order.Status.WFB,
            "Newly created order status is not 'Waiting for maker bond'",
        )
        self.assertIsInstance(
            datetime.fromisoformat(data["created_at"]),
            datetime,
            "Order creation timestamp is not datetime",
        )
        self.assertIsInstance(
            datetime.fromisoformat(data["expires_at"]),
            datetime,
            "Order expiry time is not datetime",
        )
        self.assertEqual(
            data["type"], Order.Types.BUY, "Buy order is not of type value BUY"
        )
        self.assertEqual(data["currency"], 1, "Order for USD is not of currency USD")
        self.assertIsNone(
            data["amount"], "Order with range has a non-null simple amount"
        )
        self.assertTrue(data["has_range"], "Order with range has a False has_range")
        self.assertAlmostEqual(
            float(data["min_amount"]),
            maker_form["min_amount"],
            "Order min amount does not match",
        )
        self.assertAlmostEqual(
            float(data["max_amount"]),
            maker_form["max_amount"],
            "Order max amount does not match",
        )
        self.assertEqual(
            data["payment_method"],
            maker_form["payment_method"],
            "Order payment method does not match",
        )
        self.assertEqual(
            data["escrow_duration"],
            maker_form["escrow_duration"],
            "Order escrow duration does not match",
        )
        self.assertAlmostEqual(
            float(data["bond_size"]),
            maker_form["bond_size"],
            "Order bond size does not match",
        )
        self.assertAlmostEqual(
            float(data["latitude"]),
            maker_form["latitude"],
            "Order latitude does not match",
        )
        self.assertAlmostEqual(
            float(data["longitude"]),
            maker_form["longitude"],
            "Order longitude does not match",
        )
        self.assertAlmostEqual(
            float(data["premium"]),
            maker_form["premium"],
            "Order premium does not match",
        )
        self.assertFalse(
            data["is_explicit"], "Relative pricing order has True is_explicit"
        )
        self.assertIsNone(
            data["satoshis"], "Relative pricing order has non-null Satoshis"
        )
        self.assertIsNone(data["taker"], "New order's taker is not null")

        return data

    @patch("api.lightning.cln.hold_pb2_grpc.HoldStub", MockHoldStub)
    @patch("api.lightning.lnd.lightning_pb2_grpc.LightningStub", MockLightningStub)
    @patch("api.lightning.lnd.invoices_pb2_grpc.InvoicesStub", MockInvoicesStub)
    def get_order(self, order_id, robot_index=1, first_encounter=False):
        path = "/api/order/"
        params = f"?order_id={order_id}"
        headers, _, _ = self.get_robot_auth(robot_index, first_encounter)
        response = self.client.get(path + params, **headers)

        return response

    def test_get_order_created(self):
        # Make an order
        maker_form = {
            "type": Order.Types.BUY,
            "currency": 1,
            "has_range": True,
            "min_amount": 21,
            "max_amount": 101.7,
            "payment_method": "Advcash Cash F2F",
            "is_explicit": False,
            "premium": 3.34,
            "public_duration": 69360,
            "escrow_duration": 8700,
            "bond_size": 3.5,
            "latitude": 34.7455,
            "longitude": 135.503,
        }
        order_made_response = self.create_order(maker_form, robot_index=1)
        order_made_data = json.loads(order_made_response.content.decode())

        # Maker's first order fetch. Should trigger maker bond hold invoice generation.
        response = self.get_order(order_made_data["id"])
        data = json.loads(response.content.decode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["id"], order_made_data["id"])
        self.assertTrue(
            isinstance(datetime.fromisoformat(data["created_at"]), datetime)
        )
        self.assertTrue(
            isinstance(datetime.fromisoformat(data["expires_at"]), datetime)
        )
        self.assertTrue(data["is_maker"])
        self.assertTrue(data["is_participant"])
        self.assertTrue(data["is_buyer"])
        self.assertFalse(data["is_seller"])
        self.assertEqual(data["maker_status"], "Active")
        self.assertEqual(data["status_message"], Order.Status(Order.Status.WFB).label)
        self.assertFalse(data["is_fiat_sent"])
        self.assertFalse(data["is_disputed"])
        self.assertEqual(data["ur_nick"], "MyopicRacket333")
        self.assertTrue(isinstance(data["satoshis_now"], int))
        self.assertFalse(data["maker_locked"])
        self.assertFalse(data["taker_locked"])
        self.assertFalse(data["escrow_locked"])
        self.assertEqual(
            data["bond_invoice"],
            "lntb17310n1pj552mdpp50p2utzh7mpsf3uq7u7cws4a96tj3kyq54hchdkpw8zecamx9klrqd2j2pshjmt9de6zqun9vejhyetwvdjn5gphxs6nsvfe893z6wphvfsj6dryvymj6wp5xvuz6wp5xcukvdec8yukgcf49cs9g6rfwvs8qcted4jkuapq2ay5cnpqgefy2326g5syjn3qt984253q2aq5cnz92skzqcmgv43kkgr0dcs9ymmzdafkzarnyp5kvgr5dpjjqmr0vd4jqampwvs8xatrvdjhxumxw4kzugzfwss8w6tvdssxyefqw4hxcmmrddjkggpgveskjmpfyp6kumr9wdejq7t0w5sxx6r9v96zqmmjyp3kzmnrv4kzqatwd9kxzar9wfskcmre9ccqz52xqzwzsp5hkzegrhn6kegr33z8qfxtcudaklugygdrakgyy7va0wt2qs7drfq9qyyssqc6rztchzl4m7mlulrhlcajszcl9fan8908k9n5x7gmz8g8d6ht5pj4l8r0dushq6j5s8x7yv9a5klz0kfxwy8v6ze6adyrrp4wu0q0sq3t604x",
        )
        self.assertTrue(isinstance(data["bond_satoshis"], int))

    @patch("api.lightning.lnd.invoices_pb2_grpc.InvoicesStub", MockInvoicesStub)
    def check_for_locked_bonds(self):
        # A background thread checks every 5 second the status of invoices. We invoke directly during test.
        # It will ask LND via gRPC. In our test, the request/response from LND is mocked, and it will return fake invoice status "ACCEPTED"
        follow_invoices = FollowInvoices()
        follow_invoices.follow_hold_invoices()

    def create_and_publish_order(self, maker_form, robot_index=1):
        # Make an order
        order_made_response = self.create_order(maker_form, robot_index=1)
        order_made_data = json.loads(order_made_response.content.decode())

        # Maker's first order fetch. Should trigger maker bond hold invoice generation.
        self.get_order(order_made_data["id"])

        # Check for invoice locked (the mocked LND will return ACCEPTED)
        self.check_for_locked_bonds()

        # Get order
        response = self.get_order(order_made_data["id"])
        return response

    def test_publish_order(self):
        maker_form = {
            "type": Order.Types.BUY,
            "currency": 1,
            "has_range": True,
            "min_amount": 21,
            "max_amount": 101.7,
            "payment_method": "Advcash Cash F2F",
            "is_explicit": False,
            "premium": 3.34,
            "public_duration": 69360,
            "escrow_duration": 8700,
            "bond_size": 3.5,
            "latitude": 34.7455,
            "longitude": 135.503,
        }
        # Get order
        response = self.create_and_publish_order(maker_form)
        data = json.loads(response.content.decode())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["id"], data["id"])
        self.assertEqual(data["status_message"], Order.Status(Order.Status.PUB).label)
        self.assertTrue(data["maker_locked"])
        self.assertFalse(data["taker_locked"])
        self.assertFalse(data["escrow_locked"])

        # Test what we can see with newly created robot 2 (only for public status)
        public_response = self.get_order(
            data["id"], robot_index=2, first_encounter=True
        )
        public_data = json.loads(public_response.content.decode())

        self.assertFalse(public_data["is_participant"])
        self.assertTrue(isinstance(public_data["price_now"], float))
        self.assertTrue(isinstance(data["satoshis_now"], int))
