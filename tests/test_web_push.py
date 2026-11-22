import base64
from datetime import date, datetime, timedelta
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, g, url_for
from requests import Response

from app import create_app, db
from app.models import Message, Participant, Proposal, Recipe, User, WebPushDelivery, WebPushSubscription
from app.routes import process_web_push_deliveries


class WebPushTests(TestCase):
    def setUp(self):
        self.instance = TemporaryDirectory()
        self.addCleanup(self.instance.cleanup)
        with patch('app.Flask', side_effect=lambda *args, **kwargs: Flask(
            *args, **kwargs, instance_path=self.instance.name,
        )):
            self.app = create_app()
        self.app.testing = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.client = self.app.test_client()

        sender = User(username='sender', password_hash='unused', notify_discussion=True)
        recipient = User(username='recipient', password_hash='unused', notify_discussion=True)
        db.session.add_all([sender, recipient])
        db.session.flush()
        self.sender_id = sender.id
        self.recipient_id = recipient.id
        recipe = Recipe(title='Test meal', ingredients='Test', instructions='Test')
        db.session.add(recipe)
        db.session.flush()
        proposal = Proposal(date=date.today(), recipe_id=recipe.id, proposer_id=sender.id)
        db.session.add(proposal)
        db.session.flush()
        self.proposal_id = proposal.id
        db.session.add(Participant(user_id=recipient.id, proposal_id=proposal.id))
        db.session.commit()

        public_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        self.subscription = {
            'endpoint': 'https://fcm.googleapis.com/fcm/send/old',
            'keys': {
                'p256dh': self.encode(public_key),
                'auth': self.encode(b'device secret123'),
            },
        }

    @staticmethod
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip('=')

    @staticmethod
    def response(status, retry_after=None):
        response = Response()
        response.status_code = status
        response._content = b''
        if retry_after:
            response.headers['Retry-After'] = retry_after
        return response

    def sign_in(self, user_id):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user_id)

    def subscribe(self, subscription=None, previous=None):
        return self.client.post('/push/subscribe', json={
            'subscription': subscription or self.subscription, 'previousEndpoint': previous,
        })

    def post_proposal_message(self):
        self.sign_in(self.sender_id)
        with self.app.test_request_context():
            discuss_url = url_for('main.proposal_discuss', proposal_id=self.proposal_id)
        return self.client.post(discuss_url, data={'content': 'See you at lunch!'})

    def test_rotation_is_atomic_and_preserves_same_endpoint(self):
        self.sign_in(self.recipient_id)
        old = self.subscription['endpoint']
        self.assertEqual(self.subscribe().status_code, 200)
        self.assertEqual(self.subscribe(previous=old).status_code, 200)
        shifted = {**self.subscription, 'endpoint': old.replace('/old', '/new')}
        self.assertEqual(self.subscribe({**shifted, 'keys': []}, previous=old).status_code, 400)
        self.assertEqual(WebPushSubscription.query.filter_by(endpoint=old).count(), 1)
        self.assertEqual(self.subscribe(shifted, previous=old).status_code, 200)
        self.assertEqual(WebPushSubscription.query.filter_by(endpoint=old).count(), 0)
        self.assertEqual(WebPushSubscription.query.filter_by(endpoint=shifted['endpoint']).count(), 1)

    def test_message_retry_reuses_id_and_honors_retry_after(self):
        self.sign_in(self.recipient_id)
        self.assertEqual(self.subscribe().status_code, 200)
        with patch('app.routes.PushSession.post', side_effect=[self.response(429, '120'), self.response(201)]) as send:
            self.assertEqual(self.post_proposal_message().status_code, 302)
            self.assertEqual(Message.query.filter_by(proposal_id=self.proposal_id).count(), 1)
            delivery = WebPushDelivery.query.one()
            delivery_id = delivery.id
            self.assertEqual(send.call_count, 0)
            before_attempt = datetime.utcnow()
            self.assertEqual(process_web_push_deliveries(), 1)
            self.assertGreaterEqual(delivery.due_at, before_attempt + timedelta(seconds=120))
            self.assertEqual(delivery.attempts, 1)
            self.assertEqual(process_web_push_deliveries(), 0)
            delivery.due_at = datetime.utcnow()
            db.session.commit()
            self.assertEqual(process_web_push_deliveries(), 1)
            self.assertEqual(WebPushDelivery.query.count(), 0)
            self.assertEqual([call.kwargs['headers']['topic'] for call in send.call_args_list], [delivery_id] * 2)

    def test_device_test_rejects_other_users_and_marks_expired_endpoints(self):
        self.sign_in(self.recipient_id)
        self.assertEqual(self.subscribe().status_code, 200)
        endpoint = self.subscription['endpoint']
        self.sign_in(self.sender_id)
        self.assertEqual(self.client.post('/push/test', json={'endpoint': endpoint}).status_code, 404)
        self.sign_in(self.recipient_id)
        with patch('app.routes.PushSession.post', return_value=self.response(410)):
            self.assertEqual(self.client.post('/push/test', json={'endpoint': endpoint}).status_code, 410)
        self.assertFalse(self.client.post('/push/status', json={'endpoint': endpoint}).json['registered'])
        self.assertEqual(self.subscribe().status_code, 410)
        self.assertEqual(self.post_proposal_message().status_code, 302)
        self.assertEqual(WebPushDelivery.query.count(), 0)
