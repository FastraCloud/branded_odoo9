# -*- coding= utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Need more info, contact me on: kingsleyuk2003@yahoo.com, mobile:+2348030412562
#
#    This program is free software you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http=//www.gnu.org/licenses/>.
#
##############################################################################

from openerp import models, fields, api, exceptions, _
from datetime import datetime, timedelta

class account_bank_statement(models.Model):
    
    _inherit = "account.bank.statement"
    
    input_file = fields.Binary('Imported CSV File')
    