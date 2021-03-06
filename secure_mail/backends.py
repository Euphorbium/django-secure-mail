from __future__ import with_statement

from os.path import basename

from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.mail.backends.locmem import EmailBackend as LocmemBackend
from django.core.mail.backends.filebased import EmailBackend as FileBackend
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend
from django.core.mail.message import EmailMultiAlternatives
from django.utils.encoding import smart_text
from django.utils import six

from .handlers import (handle_failed_message_encryption,
                       handle_failed_alternative_encryption,
                       handle_failed_attachment_encryption,
                       handle_failed_message_signing)
from .settings import USE_GNUPG, SIGNING_KEY_FINGERPRINT
from .utils import (EncryptionFailedError, SigningFailedError, encrypt_kwargs, get_gpg)


from .models import Address

# Create the GPG object
gpg = get_gpg()


def copy_message(msg):
    return EmailMultiAlternatives(
        to=msg.to,
        cc=msg.cc,
        bcc=msg.bcc,
        reply_to=msg.reply_to,
        from_email=msg.from_email,
        subject=msg.subject,
        body=msg.body,
        alternatives=getattr(msg, 'alternatives', []),
        attachments=msg.attachments,
        headers=msg.extra_headers,
        connection=msg.connection)


def encrypt(text, addr):
    encryption_result = gpg.encrypt(text, addr, **encrypt_kwargs)
    if not encryption_result.ok or (smart_text(encryption_result) == ""
                                    and text != ""):
        raise EncryptionFailedError("Encrypting mail to %s failed: '%s'",
                                    addr, encryption_result.status)
    return smart_text(encryption_result)


def sign(text):
    signing_result = gpg.sign(text, default_key=SIGNING_KEY_FINGERPRINT, **encrypt_kwargs)
    if not signing_result.status == 'signature created':
        raise SigningFailedError("Signing mail failed: '%s'",
                                    addr, signing_result.status)
    return smart_text(signing_result)


def encrypt_attachment(address, attachment, use_asc):
    # Attachments can either just be filenames or a
    # (filename, content, mimetype) triple
    if isinstance(attachment, six.string_types):
        filename = basename(attachment)
        mimetype = None

        # If the attachment is just a filename, open the file,
        # encrypt it, and attach it
        with open(attachment, "rb") as f:
            content = f.read()
    else:
        # Unpack attachment tuple
        filename, content, mimetype = attachment

    # Ignore attachments if they're already encrypted
    if mimetype == "application/gpg-encrypted":
        return attachment

    try:
        encrypted_content = encrypt(content, address)
    except EncryptionFailedError as e:
        # This function will need to decide what to do. Possibilities
        # include one or more of:
        #
        # * Mail admins (possibly without encrypting the message to them)
        # * Remove the offending key automatically
        # * Set the body to a blank string
        # * Set the body to the cleartext
        # * Set the body to the cleartext, with a warning message prepended
        # * Set the body to a custom error string
        # * Reraise the exception
        #
        # However, the behavior will be very site-specific, because each
        # site will have different attackers, different threat profiles,
        # different compliance requirements, and different policies.
        #
        handle_failed_attachment_encryption(e)
    else:
        if use_asc and filename is not None:
            filename += ".asc"

    return (filename, encrypted_content, "application/gpg-encrypted")


def encrypt_messages(email_messages):
    unencrypted_messages = []
    encrypted_messages = []
    for msg in email_messages:
        # Copied out of utils.py
        # Obtain a list of the recipients that have GPG keys installed
        key_addrs = dict(Address.objects.filter(address__in=msg.to)
                                        .values_list('address', 'use_asc'))

        # Encrypt emails - encrypted emails need to be sent individually,
        # while non-encrypted emails can be sent in one send. So we split
        # up each message into 1 or more parts: the unencrypted message
        # that is addressed to everybody who doesn't have a key, and a
        # separate message for people who do have keys.
        unencrypted_msg = copy_message(msg)
        unencrypted_msg.to = [addr for addr in msg.to
                              if addr not in key_addrs]
        if unencrypted_msg.to:
            unencrypted_messages.append(unencrypted_msg)

        # Make a new message object for each recipient with a key
        new_msg = copy_message(msg)
        new_msg.to = list(key_addrs.keys())

        # Encrypt the message body and all attachments for all addresses
        # we have keys for
        for address, use_asc in key_addrs.items():
            if getattr(msg, 'do_not_encrypt_this_message', False):
                unencrypted_messages.append(new_msg)
                continue

            # Replace the message body with the encrypted message body
            try:
                new_msg.body = encrypt(new_msg.body, address)
            except EncryptionFailedError as e:
                handle_failed_message_encryption(e)

            # If the message has alternatives, encrypt them all
            alternatives = []
            for alt, mimetype in getattr(new_msg, 'alternatives', []):
                # Ignore alternatives if they're already encrypted
                if mimetype == "application/gpg-encrypted":
                    alternatives.append((alt, mimetype))
                    continue

                try:
                    encrypted_alternative = encrypt(alt, address)
                except EncryptionFailedError as e:
                    handle_failed_alternative_encryption(e)
                else:
                    alternatives.append((encrypted_alternative,
                                         "application/gpg-encrypted"))
            # Replace all of the alternatives
            new_msg.alternatives = alternatives

            # Replace all unencrypted attachments with their encrypted
            # versions
            attachments = []
            for attachment in new_msg.attachments:
                attachments.append(
                    encrypt_attachment(address, attachment, use_asc))
            new_msg.attachments = attachments

            encrypted_messages.append(new_msg)

    return unencrypted_messages + encrypted_messages


def sign_messages(email_messages):
    unsigned_messages = []
    signed_messages = []
    for msg in email_messages:
        # Replace the message body with signed message body
        msg.body = sign(msg.body)
        #handle errors later
        # try:
        #     msg.body = sign(msg.body)
        # except EncryptionFailedError as e:
        #     handle_failed_message_signing(e)
    return email_messages


class EncryptingEmailBackendMixin(object):
    def send_messages(self, email_messages):
        if USE_GNUPG:
            email_messages = encrypt_messages(email_messages)
        super(EncryptingEmailBackendMixin, self)\
            .send_messages(email_messages)


class SigningEmailBackendMixin(object):
    def send_messages(self, email_messages):
        if USE_GNUPG:
            email_messages = sign_messages(email_messages)
        super(SigningEmailBackendMixin, self)\
            .send_messages(email_messages)


class EncryptingConsoleEmailBackend(EncryptingEmailBackendMixin,
                                    ConsoleBackend):
    pass


class EncryptingLocmemEmailBackend(EncryptingEmailBackendMixin,
                                   LocmemBackend):
    pass


class EncryptingFilebasedEmailBackend(EncryptingEmailBackendMixin,
                                      FileBackend):
    pass


class EncryptingSmtpEmailBackend(EncryptingEmailBackendMixin,
                                 SmtpBackend):
    pass


class SigningSmtpEmailBackend(SigningEmailBackendMixin,
                          SmtpBackend):
    pass


class SigningEmailConsoleBackend(SigningEmailBackendMixin,
                                 ConsoleBackend):
    pass
