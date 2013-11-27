# coding: utf-8
'''
Harvester for DDI2 formats
'''

#pylint: disable-msg=E1101,E0611,F0401
import datetime
import json
import logging
import lxml.etree as etree
import re
import socket
import StringIO
import urllib2

import bs4
from pylons import config
import unicodecsv as csv

from ckan.controllers.storage import BUCKET, get_ofs
from ckan.lib.base import h
import ckan.model as model
import ckan.model.authz as authz
#from ckan.lib.munge import munge_tag
from ckanext.harvest.harvesters.base import HarvesterBase
import ckanext.harvest.model as hmodel
import ckanext.kata.utils as utils
import ckanext.oaipmh.importcore as importcore

import traceback
import pprint

from ckanext.kata.utils import generate_pid

log = logging.getLogger(__name__)
socket.setdefaulttimeout(30)

AVAILABILITY_ENUM = [u'direct_download',
                     u'access_application',
                     u'access_request',
                     u'contact_owner']
AVAILABILITY_DEFAULT = AVAILABILITY_ENUM[3]
LICENCE_ID_DEFAULT =  'notspecified'
AVAILABILITY_FSD = AVAILABILITY_ENUM[2]
ACCESS_REQUEST_URL_FSD = 'http://www.fsd.uta.fi/fi/aineistot/jatkokaytto/tilaus.html'
LICENCE_ID_FSD = 'other_closed'
MAINTAINER_EMAIL_FSD = 'fsd@uta.fi'


class FieldMissingException(Exception):
    pass


def _collect_attribs(el):
    '''Collect attributes of a tag 'el' to a string with (k,v) value where k is
    the attribute name and v is the attribute value.
    '''
    astr = ""
    if el.attrs:
        for k, v in el.attrs.items():
            astr += "(%s,%s)" % (k, v)
    return astr


def _construct_csv(var, heads):
    retdict = {}
    els = var(text=False)
    varcnt = 0
    retdict['ID'] = var.get('ID', var['name'])
    for var in els:
        if var.name in ('catValu', 'catStat', 'qstn', 'catgry'):
            continue
        if var.name == 'qstn':
            valstr = var.preQTxt.string.strip() if var.preQTxt.string else None
            retdict['preQTxt'] = valstr
            valstr = var.qstnLit.string.strip() if var.qstnLit.string else None
            retdict['qstnLit'] = valstr
            valstr = var.postQTxt.string.strip() if var.postQTxt.string else None
            retdict['postQTxt'] = valstr
            valstr = var.ivuInstr.string.strip() if var.ivuInstr.string else None
            retdict['ivuInstr'] = valstr
        elif var.name.startswith('sumStat'):
            var.name = "sumStat_%s" % var['type']
            retdict[var.name] = var.string.strip()
        elif var.name == 'valrng':
            retdict['range'] = [("%s,%s" % (k, v) for k, v in var.range.attrs.iteritems())]
        elif var.name == 'invalrng':
            if var.item:
                retdict['item'] = [("%s,%s" % (k, v) for k, v in var.item.attrs.iteritems())]
        else:
            if var.name == 'labl' and 'level' in var.attrs:
                if var['level'] == 'variable' and var.string:
                    retdict['labl'] = var.string.strip()
            else:
                retdict[var.name] = var.string.strip() if var.string else None
    return retdict


def _create_code_rows(var):
    rows = []
    for cat in var('catgry', text=False, recursive=False):
        catdict = {}
        catdict['ID'] = var['ID'] if 'ID' in var else var['name']
        catdict['catValu'] = cat.catValu.string if cat.catValu else None
        catdict['labl'] = cat.labl.string if cat.labl else None
        catdict['catStat'] = cat.catStat.string if cat.catStat else None
        rows.append(catdict)
    return rows


def _get_headers():
    longest_els = ['ID',
                   'labl',
                   'preQTxt',
                   'qstnLit',
                   'postQTxt',
                   'ivuInstr',
                   'varFormat',
                   'TotlResp',
                   'range',
                   'item',
                   'sumStat_vald',
                   'sumStat_min',
                   'sumStat_max',
                   'sumStat_mean',
                   'sumStat_stdev',
                   'notes',
                   'txt']
    return longest_els


def _is_fsd(url):
    if 'fsd.uta.fi' in url:
        return True
    return False


def _access_request_URL_is_found():
    return False


def ExceptReturn(exception, returns):
    '''
    Example:
    @ExceptReturn(exception=AttributeError, returns='False_text')
def dc_metadata_reader(xml):
        print xml
        raise AttributeError('virhe')

    Jos "exception" tapahtuu dc_metadata_reader():ssa niin paluuarvo on "returns"

    >>> dc_metadata_reader('test')
    False_text
    '''
    def decorator(f):
        def call(*args, **kwargs):
            try:
                log.debug('call()')
                return f(*args, **kwargs)
            except exception as e:
                log.error('Exception occurred: %s' % e)
                return returns
        log.debug('decorator()')
        return call
    log.debug('ExceptReturn()')
    return decorator

def _future_keywords_to_labels_urls_implementation():
    ''' This old code is kept here for now if needed in future
    '''
    # JuhoL: extract, process and save keywords
    # JuhoL: keywords, match elements <keyword> <topClass>
    keywords = stdy_dscr.stdyInfo.subject(re.compile('keyword|topcClas'))
    keywords = list(set(keywords))  # JuhoL: For what? Transforming, filtering?
    idx = 0
    for kw in keywords:
        if not kw:
            continue
        #vocab = None
        #if 'vocab' in kw.attrs:
        #    vocab = kw.attrs.get("vocab", None)
        if not kw.string:
            continue
        tag = kw.string.strip()
        if tag.startswith('http://www.yso.fi'):
            tags = utils.label_list_yso(tag)
            pkg.extras['tag_source_%i' % idx] = tag
            idx += 1
        elif tag.startswith('http://') or tag.startswith('https://'):
            pkg.extras['tag_source_%i' % idx] = tag
            idx += 1
            tags = [] # URL tags break links in UI.
        else:
            tags = [tag]
        for tagi in tags:
            #pkg.add_tag_by_name(t[:100])
            tagi = tagi[:100]  # 100 char limit in DB.
            tag_obj = model.Tag.by_name(tagi)
            if not tag_obj:
                tag_obj = model.Tag(name=tagi)
                tag_obj.save()
            pkgtag = model.Session.query(model.PackageTag).filter(
                model.PackageTag.package_id==pkg.id).filter(
                model.PackageTag.tag_id==tag_obj.id).limit(1).first()
            if not pkgtag:
                pkgtag = model.PackageTag(tag=tag_obj, package=pkg)
                pkgtag.save()  # Avoids duplicates if tags has duplicates.


def _save_original_xml_and_link_as_resources(original_xml, pkg, update=False):
    if not update:
        # JuhoL: Here is created a ofs storage ie. local pairtree storage for
        # objects/blobs (eg. /opt/data/ckan/data_tree). The original xml is
        # saved to this local storage (eg.
        # /opt/data/ckan/data_tree/pairtree_root//de/fa/ul/t/obj/2013-11-05T18\:10\:19.686858/FSD1049.xml).
        # The original xml is accessible at:
        # <ckan_url>/storage/f/2013-11-05T18%3A10%3A19.686858/FSD1049.xml

        # This presumes that resources have not changed. Wrong? If something
        # has changed then technically the XML has changed and hence this may
        # have to "delete" old resources and then add new ones.
        # JuhoL: Yes, existing resources should be overwritten?
        ofs = get_ofs()
        nowstr = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')
        idno = stdy_dscr.citation.titlStmt.IDNo
        agencyxml = idno.get('agency', '') + idno.string
        label = "%s/%s.xml" % (nowstr, agencyxml,)
        ofs.put_stream(BUCKET, label, original_xml, {})
        fileurl = config.get('ckan.site_url') + h.url_for('storage_file',
                                                          label=label)
        pkg.add_resource(url=fileurl,
                         description="Original metadata record",
                         format="xml",
                         size=len(original_xml))
        # JuhoL: for FSD 'URI' leads to summary web page of data, hence format='html'
        pkg.add_resource(url=doc_citation.holdings.get('URI', ''),
                         description=title,
                         format='html')
    return True


def _save_ddi_variables_to_csv(ddi_xml, pkg, update=True):
    # JuhoL: Handle codeBook.dataDscr parts, extract data (eg. questionnaire)
    # variables etc.
    # Saves <var>...</var> elements to a csv file accessible at:
    # <ckan_url>/storage/f/2013-11-05T18%3A10%3A19.686858/1049_var.csv
    # And separately saves <catgry> elements inside <var> to a csv as a resource
    # for package.
    # Assumes that dataDscr has not changed. Valid?
    if ddi_xml.codeBook.dataDscr and not update:
        vars = ddi_xml.codeBook.dataDscr('var')  # Find all <var> elements
        heads = _get_headers()
        c_heads = ['ID', 'catValu', 'labl', 'catStat']
        f_var = StringIO.StringIO()
        c_var = StringIO.StringIO()
        varwriter = csv.DictWriter(f_var, heads)
        codewriter = csv.DictWriter(c_var, c_heads)
        heading_row = {}
        for head in heads:
            heading_row[head] = head
        c_heading_row = {}
        for head in c_heads:
            c_heading_row[head] = head
        varwriter.writerow(heading_row)
        codewriter.writerow(c_heading_row)
        for var in vars:
            try:
                varwriter.writerow(_construct_csv(var, heads))
                codewriter.writerows(_create_code_rows(var))
            except ValueError, e:
                # Assumes that the process failed. Room for retry?
                raise IOError("Failed to import DDI to CSV! %s" % e)
        f_var.flush()
        label = "%s/%s_var.csv" % (nowstr, name)
        ofs.put_stream(BUCKET, label, f_var, {})
        fileurl = config.get('ckan.site_url') + h.url_for('storage_file',
                                                          label=label)
        pkg.add_resource(url=fileurl,
                         description="Variable metadata",
                         format="csv",
                         size=f_var.len)
        label = "%s/%s_code.csv" % (nowstr, name)
        ofs.put_stream(BUCKET, label, c_var, {})
        fileurl = config.get('ckan.site_url') + h.url_for('storage_file',
                                                          label=label)
        pkg.add_resource(url=fileurl,
                         description="Variable code values",
                         format="csv",
                         size=c_var.len)
        # JuhoL: Append labels of variables ('questions') also to metas
        # TODO: change to return XPath dict of labels
        flattened_var_labels = {}
        f_var.seek(0)  # JuhoL: Set 'read cursor' to row 0
        reader = csv.DictReader(f_var)
        for var in reader:
            metas.append(var['labl'] if 'labl' in var else var['qstnLit'])
    return flattened_var_labels


def _create_group_based_on_organizations():
    # JuhoL: Create groups
    # for organizations extracted. Is this wanted? Check
    # how groups should be used currently. For group
    # stdyDscr.citation.distStmt.distrbtr or
    # docDscr.citation.prodStmt.producer or
    # stdyDscr.citation.prodStmt.producer.get('affiliation') could be more
    # appropriate.
    producers = stdy_dscr.citation.prodStmt('producer')  # this is .find_all()
    for producer in producers:
        producer = producer.string
        if producer:
            group = model.Group.by_name(producer)
            if not group:
                # JuhoL: Gives UnicodeEncodeError if contains scandics, see
                # ckanext-shibboleth plugin.py for similar fix
                group = model.Group(name=producer, description=producer,
                                    title=producer)
                group.save()
            group.add_package_by_name(pkg.name)
            authz.setup_default_user_roles(group)


def _last_statements_to_rewrite():
    # JuhoL: Add also some basic fields to pkg.extras. Why?
    # MikkoK: Parsing to extras not needed with _create_or_update_package().

    if stdy_dscr.citation.distStmt.distrbtr:
        pkg.extras['publisher'] = stdy_dscr.citation.distStmt.distrbtr.string

    # Store title in extras as well.
    pkg.extras['title_0'] = pkg.title
    pkg.extras['lang_title_0'] = pkg.language  # Guess. Good, I hope.
    if stdy_dscr.citation.titlStmt.parTitl:
        for (idx, title) in enumerate(stdy_dscr.citation.titlStmt('parTitl')):
            pkg.extras['title_%d' % (idx + 1)] = title.string
            # JuhoL: Should missing xml:lang return KeyError as in this?
            pkg.extras['lang_title_%d' % (idx + 1)] = title.attrs['xml:lang']

    # JuhoL: Authors & organizations to extras
    authorgs = []
    for value in stdy_dscr.citation.prodStmt('producer'):
        pkg.extras["producer"] = value.string
    for value in stdy_dscr.citation.rspStmt('AuthEnty'):
        org = ""
        if value.attrs.get('affiliation', None):
            org = value.attrs['affiliation']
        author = value.string
        authorgs.append((author, org))

    # JuhoL: Other contributors
    for value in stdy_dscr.citation.rspStmt('othId'):
        pkg.extras["contributor"] = value.string

    lastidx = 1
    for auth, org in authorgs:
        pkg.extras['author_%s' % lastidx] = auth
        pkg.extras['organization_%s' % lastidx] = org
        lastidx = lastidx + 1


class DataConverter:

    def __init__(self):
        self.ddi_xml = None
        self.errors = []

    def ddi2ckan(self, data, original_url=None, original_xml=None, harvest_object=None):
        try:
            self.ddi_xml = data
            return self._ddi2ckan(original_url, original_xml, harvest_object)
        #except AttributeError:
        #    raise
        except Exception as e:
            log.debug(traceback.format_exc(e))
        return False

    def _read_value(self, bs_eval_string, default=u'', mandatory_field=False):
        '''
        Evaluate values from Beautiful Soup objects.
        Returns default if evaluation failed, else return the evaluated output.
        '''
        # Make sure we are using class variables
        eval_string = bs_eval_string if bs_eval_string.startswith('self.') else 'self.' + bs_eval_string

        try:
            output = eval(eval_string)
            return output
        except AttributeError, err:
            log.debug('Unable to read value: {path}'.format(path=bs_eval_string))
            if mandatory_field:
                # TODO: Save errors to dict if mandatory field
                self.errors.append('Unable to read mandatory field: {path}'.format(path=bs_eval_string))
            return default

    def _get_events(self, stdy_dscr, orgauth):
        '''
        Parse data into events from DDI fields
        '''
        evdescr = []
        evtype = []
        evwhen = []
        evwho = []

        # Event: Collection
        ev_type_collect = self._read_value(stdy_dscr + ".stdyInfo.sumDscr('collDate', event='start')")
        data_collector = self._read_value(stdy_dscr + ".method.dataColl('dataCollector')")
        data_coll_string = u''
        for d in data_collector:
            data_coll_string += '; ' + (d.text)
        data_coll_string = data_coll_string[2:]
        for collection in ev_type_collect:
            evdescr.append({'value': u'Event automatically created at import.'})
            evtype.append({'value': u'collection'})
            evwhen.append({'value': collection.get('date')})
            evwho.append({'value': data_coll_string})

        # Event: Creation (eg. Published in publication)
        ev_type_create = self._read_value(stdy_dscr + ".citation.prodStmt.prodDate.text")
        data_creators = [ a['value'] for a in orgauth ]
        data_creator_string = '; '.join(data_creators)
        evdescr.append({'value': u'Event automatically created at import.'})
        evtype.append({'value': u'creation'})
        evwhen.append({'value': ev_type_create})
        evwho.append({'value': data_creator_string})
        # TODO: Event: Published (eg. Deposited to some public access archive)

        return (evdescr, evtype, evwhen, evwho)

    #@ExceptReturn(exception=(AttributeError, ), returns=False)
    def _ddi2ckan(self, original_url, original_xml, harvest_object):
        # JuhoL: Extract package values from bs4 object 'ddi_xml' parsed from xml
        # TODO: Use .extract() and .string.extract() function so handled elements are removed from ddi_xml.

        #self.doc_citation = ddi_xml.codeBook.docDscr.citation
        #self.stdy_dscr = ddi_xml.codeBook.stdyDscr
        doc_citation = "ddi_xml.codeBook.docDscr.citation"
        stdy_dscr = "ddi_xml.codeBook.stdyDscr"

        # Authors & organizations
        auth_entys = self._read_value(stdy_dscr + ".citation.rspStmt('AuthEnty')", mandatory_field=True)
        #auth_entys = self.stdy_dscr.citation.rspStmt('AuthEnty')
        orgauth = []
        for a in auth_entys:
            orgauth.append({'org': a.get('affiliation'), 'value': a.text})

        # Availability
        availability = AVAILABILITY_DEFAULT
        if _access_request_URL_is_found():
            availability = 'direct_download'
        if _is_fsd(original_url):
            availability = AVAILABILITY_FSD

        # Keywords
        keywords = self._read_value(stdy_dscr + ".stdyInfo.subject.get_text(',', strip=True)", mandatory_field=True)

        # Language
        # TODO: Where/how to extract multiple languages: 'language': u'eng, fin, swe' ?
        language = self._read_value("ddi_xml.codeBook.get('xml:lang')", mandatory_field=True)

        # Titles
        titles = self._read_value(stdy_dscr + ".citation.titlStmt(['titl', 'parTitl'])", mandatory_field=False)
        if not titles:
            titles =  self._read_value(doc_citation + ".titlStmt(['titl', 'parTitl'])", mandatory_field=True)

        #langtitle=[dict(lang=a.get('xml:lang', ''), value=a.text) for a in titles]
        langtitle=[dict(lang='fin', value=a.text) for a in titles]

        # License
        # TODO: Extract prettier output. Should we check that element contains something?
        license_url = self._read_value(stdy_dscr + ".dataAccs.useStmt.get_text(separator=u' ')", mandatory_field=False)
        if _is_fsd(original_url):
            license_id = LICENCE_ID_FSD
        else:
            license_id = LICENCE_ID_DEFAULT

        # Publisher (maintainer in database)
        maintainer = self._read_value(stdy_dscr + ".citation.distStmt('contact')", mandatory_field=False) or \
                     self._read_value(stdy_dscr + ".citation.distStmt('distrbtr')", mandatory_field=False) or \
                     self._read_value(doc_citation + ".prodStmt('producer')", mandatory_field=True)
        if maintainer and maintainer[0].text:
            maintainer = maintainer[0].text
        else:
            maintainer = self._read_value(stdy_dscr + ".citation.prodStmt.producer.get('affiliation')", mandatory_field=True)
        if _is_fsd(original_url) or 'misanthropy.kapsi.fi' in original_url:  # DEBUG, remove after 'or'
            maintainer_email = MAINTAINER_EMAIL_FSD
            # TODO: Allow trying other email also in FSD metadata
        else:
            maintainer_email = self._read_value(stdy_dscr + ".citation.distStmt.contact.get('email')", mandatory_field=True)

        # Modified date
        version = self._read_value(stdy_dscr + ".citation('prodDate')", mandatory_field=False) or \
                  self._read_value(stdy_dscr + ".citation('depDate')", mandatory_field=True)
        version = version[0].get('date')

        # Name
        name_prefix = self._read_value(stdy_dscr + ".citation.titlStmt.IDNo.get('agency')", mandatory_field=False)
        name_id = self._read_value(stdy_dscr + ".citation.titlStmt.IDNo.text", mandatory_field=False)

        if not name_prefix:
            name_prefix = self._read_value(doc_citation + ".titlStmt.IDNo.get('agency')", mandatory_field=True)

        if not name_id:
            self._read_value(doc_citation + ".titlStmt.IDNo.text", mandatory_field=True)

        # JuhoL: if we generate pkg.name we cannot reharvest + end up adding
        # same harvest object at each reharvest
        # name = utils.generate_pid()
        name = name_prefix + name_id
        log.debug('Name: {namn}'.format(namn=name))

        # Owner
        owner = self._read_value(stdy_dscr + ".citation.prodStmt.producer.text") or \
                self._read_value(stdy_dscr + ".citation.rspStmt.AuthEnty.text") or \
                self._read_value(doc_citation + ".prodStmt.producer.string", mandatory_field=True)


        # Read optional metadata fields:

        # Availability
        if _is_fsd(original_url):
            access_request_url=ACCESS_REQUEST_URL_FSD
        else:
            access_request_url=u''

        # Contact
        contact_phone = self._read_value(doc_citation + ".holdings.get('callno')") or \
                        self._read_value(stdy_dscr + ".citation.holdings.get('callno')")

        contact_URL = self._read_value( stdy_dscr + ".dataAccs.setAvail.accsPlac.get('URI')") or \
                      self._read_value( stdy_dscr + ".citation.distStmt.contact.get('URI')") or \
                      self._read_value( stdy_dscr + ".citation.distStmt.distrbtr.get('URI')")

        # Description
        description_array = self._read_value(stdy_dscr + ".stdyInfo.abstract('p')")
        if not description_array:
            description_array = self._read_value(stdy_dscr + ".citation.serStmt.serInfo('p')")

        notes = '\r\n\r\n'.join([description.string for
                               description in description_array])

        evdescr, evtype, evwhen, evwho = self._get_events(stdy_dscr, orgauth)

        # Flatten rest to 'XPath/path/to/element': 'value' pairs
        # TODO: Result is large, review.
        etree_xml = etree.fromstring(original_xml)
        #lroot = etree_xml.getroot()
        flattened_ddi = importcore.generic_xml_metadata_reader(etree_xml.find('.//{*}docDscr'))
        xpath_dict = flattened_ddi.getMap()
        flattened_ddi = importcore.generic_xml_metadata_reader(etree_xml.find('.//{*}stdyDscr'))
        xpath_dict.update(flattened_ddi.getMap())

        package_dict = dict(
            access_application_URL=u'',   ## JuhoL: changed 'accessRights' to 'access_application_URL
            access_request_URL=unicode(access_request_url),
            # algorithm=NotImplemented,   ## To be implemented straight in 'resources'
            availability=unicode(availability),
            #contact_phone=unicode(contact_phone),
            contact_URL=unicode(contact_URL),
            # direct_download_URL=u'http://helsinki.fi/data-on-taalla',  ## To be implemented straight in 'resources
            discipline=u'Tilastotiede',
            evdescr=evdescr or [],
            evtype=evtype or [],
            evwhen=evwhen or [],
            evwho=evwho or [],
            geographic_coverage=u'Espoo (city),Keilaniemi (populated place)',
            groups=[],
            id=generate_pid(),
            langtitle=langtitle,
            langdis=u'True',  ### HUOMAA!
            #language=language,
            license_URL=license_url,
            license_id=license_id,
            maintainer=maintainer,   ## JuhoL: changed 'publisher' to 'maintainer'
            maintainer_email=maintainer_email,
            # mimetype=u'application/csv',  ## To be implemented straight in 'resources
            name=filter(unicode.isalnum, name).lower(),
            notes=notes or u'',
            orgauth=orgauth,
            owner=owner,
            projdis=u'False',   ### HUOMAA!
            project_funder=u'Roope Rahoittaja',
            project_funding=u'1234-rahoitusp\xe4\xe4t\xf6snumero',
            project_homepage=u'http://www.rahoittajan.kotisivu.fi/',
            project_name=u'Rahoittajan Projekti',
            #resources=[{'algorithm': u'MD5',
            #            'hash': u'f60e586509d99944e2d62f31979a802f',
            #            'mimetype': u'application/csv',
            #            'resource_type': 'dataset',
            #            'url': u'http://aineiston.osoite.fi/tiedosto.csv'},
            #           {'algorithm': u'',
            #            'hash': u'',
            #            'mimetype': u'',
            #            'resource_type': 'dataset',
            #            'url': u''}],
            tag_string=keywords,
            temporal_coverage_begin=u'1976-11-06T00:00:00Z',
            temporal_coverage_end=u'2003-11-06T00:00:00Z',
            title='',   # Must exist in package dict
            #type='dataset',
            version=version,
            version_PID=u'Aineistoversion-tunniste-PID'   ## JuhoL: added underscore '_'
        )
        #package_dict['extras'] = flattened_ddi
        #package_dict['extras'].update(_save_ddi_variables_to_csv(ddi_xml, somepkg))


        # Vanhojen koodien järjestys:
        #_save_original_xml_and_link_as_resources()
        #_save_ddi_variables_to_csv()
        #_create_group_based_on_organizations()
        #_last_statements_to_rewrite()

        # JuhoL: Set harvest object to some end state and commit
        if harvest_object != None:
            #harvest_object.package_id = pkg.id
            harvest_object.content = None
            harvest_object.current = True
        #model.repo.commit()
        #return pkg.id

        # TODO: Check if we received errors from parsing minimum metadata model fields and throw an exception if so.

        if self.errors:
            raise FieldMissingException(tuple(self.errors))

        return package_dict


#def ddi32ckan(ddi_xml, original_xml, original_url=None, harvest_object=None):
#    try:
#        return _ddi32ckan(ddi_xml, original_xml, original_url, harvest_object)
#    except Exception as e:
#        log.debug(traceback.format_exc(e))
#    return False
#
#def _ddi32ckan(ddi_xml, original_xml, original_url, harvest_object):
#    model.repo.new_revision()
#    ddiroot = ddi_xml.DDIInstance
#    main_cit = ddiroot.Citation
#    study_info = ddiroot('StudyUnit')[-1]
#    idx = 0
#    authorgs = []
#    pkg = model.Package.get(study_info.attrs['id'])
#    if not pkg:
#        pkg = model.Package(name=study_info.attrs['id'])
#        pkg.id = ddiroot.attrs['id']
#        # This presumes that resources have not changed. Wrong? If something
#        # has changed then technically the XML has chnaged and hence this may
#        # have to "delete" old resources and then add new ones.
#        ofs = get_ofs()
#        nowstr = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')
#        label = "%s/%s.xml" % (nowstr, study_info.attrs['id'],)
#        ofs.put_stream(BUCKET, label, original_xml, {})
#        fileurl = config.get('ckan.site_url') + h.url_for('storage_file',
#            label=label)
#        pkg.add_resource(url=fileurl, description="Original metadata record",
#            format="xml", size=len(original_xml))
#        # What the URI should be?
#        #pkg.add_resource(url=doc_citation.holdings['URI']\
#        #                 if 'URI' in doc_citation.holdings else '',
#        #                 description=title)
#    pkg.version = main_cit.PublicationDate.SimpleDate.string
#    for title in main_cit('Title'):
#        pkg.extras['title_%d' % idx] = title.string
#        pkg.extras['lang_title_%d' % idx] = title.attrs['xml:lang']
#        idx += 1
#    for title in study_info.Citation('Title'):
#        pkg.extras['title_%d' % idx] = title.string
#        pkg.extras['lang_title_%d' % idx] = title.attrs['xml:lang']
#        idx += 1
#    for value in study_info.Citation('Creator'):
#        org = ""
#        if value.attrs.get('affiliation', None):
#            org = value.attrs['affiliation']
#        author = value.string
#        authorgs.append((author, org))
#    pkg.author = authorgs[0][0]
#    pkg.maintainer = study_info.Citation.Publisher.string
#    lastidx = 0
#    for auth, org in authorgs:
#        pkg.extras['author_%s' % lastidx] = auth
#        pkg.extras['organization_%s' % lastidx] = org
#        lastidx = lastidx + 1
#    pkg.extras["licenseURL"] = study_info.Citation.Copyright.string
#    pkg.notes = "".join([unicode(repr(chi).replace('\n', '<br />'), 'utf8')\
#                         for chi in study_info.Abstract.Content.children])
#    for kw in study_info.Coverage.TopicalCoverage('Keyword'):
#        pkg.add_tag_by_name(kw.string)
#    pkg.extras['contributor'] = study_info.Citation.Contributor.string
#    pkg.extras['publisher'] = study_info.Citation.Publisher.string
#    pkg.save()
#    if harvest_object:
#        harvest_object.package_id = pkg.id
#        harvest_object.content = None
#        harvest_object.current = True
#        harvest_object.save()
#    model.repo.commit()
#    return pkg.id
#
