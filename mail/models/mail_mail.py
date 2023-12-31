# -*- coding: utf-8 -*-

import base64
import logging
from email.utils import formataddr

import psycopg2

from openerp import _, api, fields, models
from openerp import tools
from openerp.addons.base.ir.ir_mail_server import MailDeliveryException
from openerp.tools.safe_eval import safe_eval as eval

_logger = logging.getLogger(__name__)


class MailMail(models.Model):
    """ Model holding RFC2822 email messages to send. This model also provides
        facilities to queue and send new email messages.  """
    _name = 'mail.mail'
    _description = 'Outgoing Mails'
    _inherits = {'mail.message': 'mail_message_id'}
    _order = 'id desc'
    _rec_name = 'subject'

    # content
    mail_message_id = fields.Many2one('mail.message', 'Message', required=True, ondelete='cascade', index=True, auto_join=True)
    body_html = fields.Text('Rich-text Contents', help="Rich-text/HTML message")
    references = fields.Text('References', help='Message references, such as identifiers of previous messages', readonly=1)
    headers = fields.Text('Headers', copy=False)
    # Auto-detected based on create() - if 'mail_message_id' was passed then this mail is a notification
    # and during unlink() we will not cascade delete the parent and its attachments
    notification = fields.Boolean('Is Notification', help='Mail has been created to notify people of an existing mail.message')
    # recipients
    email_to = fields.Text('To', help='Message recipients (emails)')
    email_cc = fields.Char('Cc', help='Carbon copy message recipients')
    recipient_ids = fields.Many2many('res.partner', string='To (Partners)')
    # process
    state = fields.Selection([
        ('outgoing', 'Outgoing'),
        ('sent', 'Sent'),
        ('received', 'Received'),
        ('exception', 'Delivery Failed'),
        ('cancel', 'Cancelled'),
    ], 'Status', readonly=True, copy=False, default='outgoing')
    auto_delete = fields.Boolean(
        'Auto Delete',
        help="Permanently delete this email after sending it, to save space")
    failure_reason = fields.Text(
        'Failure Reason', readonly=1,
        help="Failure reason. This is usually the exception thrown by the email server, stored to ease the debugging of mailing issues.")

    @api.model
    def create(self, values):
        # notification field: if not set, set if mail comes from an existing mail.message
        if 'notification' not in values and values.get('mail_message_id'):
            values['notification'] = True
        return super(MailMail, self).create(values)

    @api.multi
    def unlink(self):
        # cascade-delete the parent message for all mails that are not created for a notification
        to_cascade = self.search([('notification', '=', False), ('id', 'in', self.ids)]).mapped('mail_message_id')
        res = super(MailMail, self).unlink()
        to_cascade.unlink()
        return res

    @api.model
    def default_get(self, fields):
        # protection for `default_type` values leaking from menu action context (e.g. for invoices)
        # To remove when automatic context propagation is removed in web client
        if self._context.get('default_type') not in self._all_columns['message_type'].column.selection:
            self = self.with_context(dict(self._context, default_type=None))
        return super(MailMail, self).default_get(fields)

    @api.multi
    def mark_outgoing(self):
        return self.write({'state': 'outgoing'})

    @api.multi
    def cancel(self):
        return self.write({'state': 'cancel'})

    @api.model
    def process_email_queue(self, ids=None):
        """Send immediately queued messages, committing after each
           message is sent - this is not transactional and should
           not be called during another transaction!

           :param list ids: optional list of emails ids to send. If passed
                            no search is performed, and these ids are used
                            instead.
           :param dict context: if a 'filters' key is present in context,
                                this value will be used as an additional
                                filter to further restrict the outgoing
                                messages to send (by default all 'outgoing'
                                messages are sent).
        """
        if not self.ids:
            filters = [('state', '=', 'outgoing'),('date','>',self.env.user.company_id.x_time_start)]
            if 'filters' in self._context:
                filters.extend(self._context['filters'])
            ids = self.search(filters).ids
        res = None
        print(ids)
        try:
            # Force auto-commit - this is meant to be called by
            # the scheduler, and we can't allow rolling back the status
            # of previously sent emails!
            res = self.browse(ids).send(auto_commit=True)
        except Exception:
            _logger.exception("Failed processing mail queue")
        return res

    @api.cr_uid_context
    def _postprocess_sent_message(self, cr, uid, mail, context=None, mail_sent=True):
        return True

    @api.multi
    def _postprocess_sent_message_v9(self, mail_sent=True):
        """Perform any post-processing necessary after sending ``mail``
        successfully, including deleting it completely along with its
        attachment if the ``auto_delete`` flag of the mail was set.
        Overridden by subclasses for extra post-processing behaviors.

        :param browse_record mail: the mail that was just sent
        :return: True
        """
        # Compat mode until v9
        for mail in self:
            self._postprocess_sent_message(mail, mail_sent=mail_sent)

        if mail_sent:
            self.sudo().filtered(lambda self: self.auto_delete).unlink()
        return True

    # ------------------------------------------------------
    # mail_mail formatting, tools and send mechanism
    # ------------------------------------------------------

    @api.multi
    def send_get_mail_body(self, partner=None):
        """Return a specific ir_email body. The main purpose of this method
        is to be inherited to add custom content depending on some module."""
        self.ensure_one()
        body = self.body_html or ''
        return body

    @api.multi
    def send_get_mail_to(self, partner=None):
        """Forge the email_to with the following heuristic:
          - if 'partner', recipient specific (Partner Name <email>)
          - else fallback on mail.email_to splitting """
        self.ensure_one()
        if partner:
            email_to = [formataddr((partner.name, partner.email))]
        else:
            email_to = tools.email_split_and_format(self.email_to)
        return email_to

    @api.multi
    def send_get_email_dict(self, partner=None):
        """Return a dictionary for specific email values, depending on a
        partner, or generic to the whole recipients given by mail.email_to.

            :param browse_record mail: mail.mail browse_record
            :param browse_record partner: specific recipient partner
        """
        self.ensure_one()
        body = self.send_get_mail_body(partner=partner)
        body_alternative = tools.html2plaintext(body)
        res = {
            'body': body,
            'body_alternative': body_alternative,
            'email_to': self.send_get_mail_to(partner=partner),
        }

        return res

    @api.multi
    def send(self, auto_commit=False, raise_exception=False):
        """ Sends the selected emails immediately, ignoring their current
            state (mails that have already been sent should not be passed
            unless they should actually be re-sent).
            Emails successfully delivered are marked as 'sent', and those
            that fail to be deliver are marked as 'exception', and the
            corresponding error mail is output in the server logs.

            :param bool auto_commit: whether to force a commit of the mail status
                after sending each mail (meant only for scheduler processing);
                should never be True during normal transactions (default: False)
            :param bool raise_exception: whether to raise an exception if the
                email sending process has failed
            :return: True
        """
        IrMailServer = self.env['ir.mail_server']

        for mail in self:
            try:
                # TDE note: remove me when model_id field is present on mail.message - done here to avoid doing it multiple times in the sub method
                if mail.model:
                    model = self.env['ir.model'].sudo().search([('model', '=', mail.model)])[0]
                else:
                    model = None
                if model:
                    mail = mail.with_context(model_name=model.name)

                # load attachment binary data with a separate read(), as prefetching all
                # `datas` (binary field) could bloat the browse cache, triggerring
                # soft/hard mem limits with temporary data.
                attachments = [(a['datas_fname'], base64.b64decode(a['datas']))
                               for a in mail.attachment_ids.sudo().read(['datas_fname', 'datas'])]

                # specific behavior to customize the send email for notified partners
                email_list = []
                if mail.email_to:
                    email_list.append(mail.send_get_email_dict())
                for partner in mail.recipient_ids:
                    email_list.append(mail.send_get_email_dict(partner=partner))

                # headers
                headers = {}
                bounce_alias = self.env['ir.config_parameter'].get_param("mail.bounce.alias")
                catchall_domain = self.env['ir.config_parameter'].get_param("mail.catchall.domain")
                if bounce_alias and catchall_domain:
                    if mail.model and mail.res_id:
                        headers['Return-Path'] = '%s-%d-%s-%d@%s' % (bounce_alias, mail.id, mail.model, mail.res_id, catchall_domain)
                    else:
                        headers['Return-Path'] = '%s-%d@%s' % (bounce_alias, mail.id, catchall_domain)
                if mail.headers:
                    try:
                        headers.update(eval(mail.headers))
                    except Exception:
                        pass

                # Writing on the mail object may fail (e.g. lock on user) which
                # would trigger a rollback *after* actually sending the email.
                # To avoid sending twice the same email, provoke the failure earlier
                mail.write({
                    'state': 'exception',
                    'failure_reason': _('Error without exception. Probably due do sending an email without computed recipients.'),
                })
                mail_sent = False

                # build an RFC2822 email.message.Message object and send it without queuing
                res = None
                for email in email_list:
                    #print(email)
		    #print(email.get('email_to'))
                    msg = IrMailServer.build_email(
                        email_from=mail.email_from,
                        email_to=email.get('email_to'),
                        subject=mail.subject,
                        body=email.get('body'),
                        body_alternative=email.get('body_alternative'),
                        email_cc=tools.email_split(mail.email_cc),
                        reply_to=mail.reply_to,
                        attachments=attachments,
                        message_id=mail.message_id,
                        references=mail.references,
                        object_id=mail.res_id and ('%s-%s' % (mail.res_id, mail.model)),
                        subtype='html',
                        subtype_alternative='plain',
                        headers=headers)
                    try:
			#print(mail.mail_server_id)
                        res = IrMailServer.send_email(msg, mail_server_id=mail.mail_server_id.id)
                        if res:
			   print("psdfk")
                    except AssertionError as error:
                        if error.message == IrMailServer.NO_VALID_RECIPIENT:
                            # No valid recipient found for this particular
                            # mail item -> ignore error to avoid blocking
                            # delivery to next recipients, if any. If this is
                            # the only recipient, the mail will show as failed.
                            _logger.info("Ignoring invalid recipients for mail.mail %s: %s",
                                         mail.message_id, email.get('email_to'))
                        else:
                          
                            raise
                if res:
		    
                    mail.write({'state': 'sent', 'message_id': res, 'failure_reason': False})
                    mail_sent = True

                # /!\ can't use mail.state here, as mail.refresh() will cause an error
                # see revid:odo@openerp.com-20120622152536-42b2s28lvdv3odyr in 6.1
                if mail_sent:
                    _logger.info('Mail with ID %r and Message-Id %r successfully sent', mail.id, mail.message_id)
                mail._postprocess_sent_message_v9(mail_sent=mail_sent)
            except MemoryError:
                # prevent catching transient MemoryErrors, bubble up to notify user or abort cron job
                # instead of marking the mail as failed
                _logger.exception(
                    'MemoryError while processing mail with ID %r and Msg-Id %r. Consider raising the --limit-memory-hard startup option',
                    mail.id, mail.message_id)
                raise
            except psycopg2.Error:
                # If an error with the database occurs, chances are that the cursor is unusable.
                # This will lead to an `psycopg2.InternalError` being raised when trying to write
                # `state`, shadowing the original exception and forbid a retry on concurrent
                # update. Let's bubble it.
                raise
            except Exception as e:
                failure_reason = tools.ustr(e)
                _logger.exception('failed sending mail (id: %s) due to %s', mail.id, failure_reason)
                mail.write({'state': 'exception', 'failure_reason': failure_reason})
                mail._postprocess_sent_message_v9(mail_sent=False)
                if raise_exception:
                    if isinstance(e, AssertionError):
                        # get the args of the original error, wrap into a value and throw a MailDeliveryException
                        # that is an except_orm, with name and value as arguments
                        value = '. '.join(e.args)
                        raise MailDeliveryException(_("Mail Delivery Failed"), value)
                    raise

            if auto_commit is True:
                self._cr.commit()
        return True
