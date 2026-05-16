import unittest
import time
from src.services.stream_tickets import create_ticket, redeem_ticket, _tickets

class TestStreamTickets(unittest.TestCase):
    def setUp(self):
        _tickets.clear()

    def test_create_and_redeem(self):
        user_id = 123
        ticket = create_ticket(user_id)
        self.assertIsNotNone(ticket)
        self.assertIn(ticket, _tickets)

        redeemed_user_id = redeem_ticket(ticket)
        self.assertEqual(user_id, redeemed_user_id)
        self.assertNotIn(ticket, _tickets)

    def test_single_use(self):
        user_id = 123
        ticket = create_ticket(user_id)
        redeem_ticket(ticket)
        second_redeem = redeem_ticket(ticket)
        self.assertIsNone(second_redeem)

    def test_invalid_ticket(self):
        self.assertIsNone(redeem_ticket("invalid-ticket"))

    def test_expiration(self):
        # We can't easily mock time.time() here without more complex setup
        # but we can manually manipulate the ticket store for testing
        user_id = 123
        ticket = create_ticket(user_id)
        _tickets[ticket]["expires"] = time.time() - 1 # expired

        self.assertIsNone(redeem_ticket(ticket))

if __name__ == "__main__":
    unittest.main()
