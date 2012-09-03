# coding: utf-8
'''
Tests for DDI harvester
'''
# pylint: disable=E1101,C1101,C0111
import logging
import unittest
import mock
import urllib2
from StringIO import StringIO
import json
import uuid
import pprint

import testdata

from lxml import etree

from ckan.model import Session, Package, User
from ckan.lib.helpers import url_for
from ckan.tests.functional.base import FunctionalTestCase
from ckan.tests import CreateTestData
from ckan.logic.auth.get import package_show, group_show
from ckan import model


from ckanext.ddi.harvester import DDIHarvester
from ckanext.harvest.model import HarvestJob, HarvestSource, HarvestObject, \
                                  HarvestObjectError, HarvestGatherError, setup
from sqlalchemy.ext.associationproxy import _AssociationDict

log = logging.getLogger(__file__)
realopen = urllib2.urlopen


class TestDDIHarvester(unittest.TestCase, FunctionalTestCase):

    @classmethod
    def setup_class(self):
        username = u'testlogin2'
        password = u'letmein'
        CreateTestData.create_user(name=username,
                                   password=password)
        # do the login
        offset = url_for(controller='user', action='login')
        res = self.app.get(offset)
        fv = res.forms['login']
        fv['login'] = str(username)
        fv['password'] = str(password)
        fv['remember'] = True
        res = fv.submit()
        setup()

    @classmethod
    def teardown_class(self):
        Session.remove()

    def _create_harvester(self, config=True):
        harv = DDIHarvester()
        harv.config = "{}"
        harvest_job = HarvestJob()
        harvest_job.source = HarvestSource()
        harvest_job.source.title = "Test"
        harvest_job.source.url = "http://foo"
        if config:
            harvest_job.source.config = ''
        else:
            harvest_job.source.config = None
        harvest_job.source.type = "DDI"
        Session.add(harvest_job)
        return harv, harvest_job

    def test_harvester_info(self):
        harv, _ = self._create_harvester()
        self.assert_(isinstance(harv.info(), dict))
        self.assert_(harv.validate_config(harv.config))
        harv, _ = self._create_harvester(config=False)

    def test_harvester_create(self):
        harv, job = self._create_harvester()
        self.assert_(harv)
        self.assert_(job)
        self.assert_(job.source)
        self.assert_(job.source.title == "Test")

    def test_harvester_urlerror(self):
        harv, job = self._create_harvester()
        urllib2.urlopen = realopen
        self.assert_(harv.gather_stage(job) == None)
        errs = Session.query(HarvestGatherError).all()
        self.assert_(len(errs) == 1)
        harv_obj = HarvestObject()
        harv_obj.job = job
        harv_obj.content = json.dumps({'url': "http://foo"})
        self.assert_(harv.fetch_stage(harv_obj) == False)
        errs = Session.query(HarvestObjectError).all()
        # XML error and URL error, also the lack of url in content
        self.assert_(len(errs) == 2)

    def test_harvester_gather(self):
        harv, job = self._create_harvester()
        res = """
        http://www.fsd.uta.fi/fi/aineistot/luettelo/FSD0115/FSD0115.xml
        """
        urllib2.urlopen = mock.Mock(return_value=StringIO(res))
        gathered = harv.gather_stage(job)
        self.assert_(len(gathered) != 0)
        uid = uuid.UUID(gathered[0])
        self.assert_(str(uid))
        harv, job = self._create_harvester(config=False)
        harv.gather_stage(job)

    def test_harvester_fetch(self):
        harv, job = self._create_harvester()
        res = """
        http://www.fsd.uta.fi/fi/aineistot/luettelo/FSD0115/FSD0115.xml
        """
        urllib2.urlopen = mock.Mock(return_value=StringIO(res))
        gathered = harv.gather_stage(job)
        urllib2.urlopen = mock.Mock(return_value=StringIO(testdata.nr1))
        harvest_obj = HarvestObject.get(gathered[0])
        self.assert_(harv.fetch_stage(harvest_obj))
        self.assert_(isinstance(json.loads(harvest_obj.content), dict))
        result = json.loads(harvest_obj.content)
        self.assert_("stdyDscr" in result['xml']['codeBook'])
        urllib2.urlopen = mock.Mock(return_value=StringIO(testdata.foobar))
        harvest_obj = HarvestObject.get(gathered[0])
        self.assert_(not harv.fetch_stage(harvest_obj))

    def test_harvester_import(self):
        harv, job = self._create_harvester()
        res = "http://www.fsd.uta.fi/fi/aineistot/luettelo/FSD0115/FSD0115.xml"
        urllib2.urlopen = mock.Mock(return_value=StringIO(res))
        gathered = harv.gather_stage(job)
        urllib2.urlopen = mock.Mock(return_value=StringIO(testdata.nr1))
        harvest_obj = HarvestObject.get(gathered[0])
        self.assert_(harv.fetch_stage(harvest_obj))
        self.assert_(isinstance(json.loads(harvest_obj.content), dict))
        self.assert_(harv.import_stage(harvest_obj))
        self.assert_(len(Session.query(Package).all()) == 1)

        # Lets see if the package is ok, according to test data
        log.debug(Session.query(Package).all())
        pkg = Session.query(Package).filter(Package.title == "Puolueiden ajankohtaistutkimus 1981").one()
        self.assert_(pkg.title == "Puolueiden ajankohtaistutkimus 1981")
        self.assert_(len(pkg.get_groups()) == 2)
        self.assert_(len(pkg.resources) == 1)
        self.assert_(len(pkg.get_tags()) == 9)
        self.assert_(pkg.url == "http://www.fsd.uta.fi/fi/aineistot/luettelo/FSD0115/FSD0115.xml")
        self.assert_(isinstance(pkg.extras, _AssociationDict))
        self.assert_(len(pkg.extras.items()) > 1)
        # Test if we have a variable with a standard deviation and mean, with min and max
        # All other variables might not make any sense.
        self.assert_('Q43_2' in pkg.extras)

        urllib2.urlopen = mock.Mock(return_value=StringIO(testdata.nr2))
        harvest_obj = HarvestObject.get(gathered[0])
        harvest_obj.content = json.dumps({'url': 'http://foo'})
        self.assert_(harv.fetch_stage(harvest_obj))
        self.assert_(isinstance(json.loads(harvest_obj.content), dict))
        self.assert_(harv.import_stage(harvest_obj))
        self.assert_(len(Session.query(Package).all()) == 2)

        # Test user access
        user = User.get('testlogin2')
        grp = pkg.get_groups()[0]
        context = {'user': user.name, 'model': model}
        data_dict = {'id': pkg.id}
        auth_dict = package_show(context, data_dict)
        self.assert_(auth_dict['success'])
        data_dict = {'id': grp.id}
        context = {'user': '', 'model': model}
        auth_dict = group_show(context, data_dict)
        self.assert_(auth_dict['success'])

    def test_zfaulty_xml_1088(self):
        harv, job = self._create_harvester()
        res = "http://www.fsd.uta.fi/fi/aineistot/luettelo/FSD0115/FSD0115.xml"
        urllib2.urlopen = mock.Mock(return_value=StringIO(res))
        gathered = harv.gather_stage(job)
        urllib2.urlopen = mock.Mock(return_value=open("FSD1088.xml"))
        harvest_obj = HarvestObject.get(gathered[0])
        self.assert_(harv.fetch_stage(harvest_obj))
        self.assert_(harv.import_stage(harvest_obj))

    def test_zfaulty_xml_1050(self):
        harv, job = self._create_harvester()
        res = "http://www.fsd.uta.fi/fi/aineistot/luettelo/FSD0115/FSD0115.xml"
        urllib2.urlopen = mock.Mock(return_value=StringIO(res))
        gathered = harv.gather_stage(job)
        urllib2.urlopen = mock.Mock(return_value=open("FSD1050.xml"))
        harvest_obj = HarvestObject.get(gathered[0])
        self.assert_(harv.fetch_stage(harvest_obj))
        self.assert_(harv.import_stage(harvest_obj))

