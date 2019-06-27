# =============================================================================
# OWSLib. Copyright (C) 2005 Sean C. Gillies
#
# Contact email: sgillies@frii.com
#
# $Id: wfs.py 503 2006-02-01 17:09:12Z dokai $
# =============================================================================

from io import StringIO

from owslib import util

from urllib.parse import urlencode
from owslib.util import testXMLValue, extract_xml_list, ServiceException, xmltag_split, Authentication, openURL, log
from owslib.etree import etree
from owslib.fgdc import Metadata
from owslib.iso import MD_Metadata
from owslib.crs import Crs
from owslib.namespaces import Namespaces
from owslib.feature.schema import get_schema
from owslib.feature.common import WFSCapabilitiesReader, \
    AbstractContentMetadata

import pyproj

n = Namespaces()
WFS_NAMESPACE = n.get_namespace("wfs")
OGC_NAMESPACE = n.get_namespace("ogc")


#TODO: use nspath in util.py
def nspath(path, ns=WFS_NAMESPACE):
    """
    Prefix the given path with the given namespace identifier.

    Parameters
    ----------
    path : string
        ElementTree API Compatible path expression

    ns : string
        The XML namespace. Defaults to WFS namespace.
    """
    components = []
    for component in path.split("/"):
        if component != '*':
            component = "{%s}%s" % (ns, component)
        components.append(component)
    return "/".join(components)


class WebFeatureService_1_0_0(object):
    """Abstraction for OGC Web Feature Service (WFS).

    Implements IWebFeatureService.
    """
    def __new__(self,url, version, xml, parse_remote_metadata=False, timeout=30,
                username=None, password=None, auth=None):
        """ overridden __new__ method

        @type url: string
        @param url: url of WFS capabilities document
        @type xml: string
        @param xml: elementtree object
        @type parse_remote_metadata: boolean
        @param parse_remote_metadata: whether to fully process MetadataURL elements
        @param timeout: time (in seconds) after which requests should timeout
        @param username: service authentication username
        @param password: service authentication password
        @param auth: instance of owslib.util.Authentication
        @return: initialized WebFeatureService_1_0_0 object
        """
        obj=object.__new__(self)
        obj.__init__(url, version, xml, parse_remote_metadata, timeout,
                     username=username, password=password, auth=auth)
        return obj

    def __getitem__(self,name):
        ''' check contents dictionary to allow dict like access to service layers'''
        if name in self.__getattribute__('contents').keys():
            return self.__getattribute__('contents')[name]
        else:
            raise KeyError("No content named %s" % name)

    def __init__(self, url, version, xml=None, parse_remote_metadata=False, timeout=30,
                 username=None, password=None, auth=None):
        """Initialize."""
        if auth:
            if username:
                auth.username = username
            if password:
                auth.password = password
        self.url = url
        self.version = version
        self.timeout = timeout
        self.auth = auth or Authentication(username, password)
        self._capabilities = None
        reader = WFSCapabilitiesReader(self.version, auth=self.auth)
        if xml:
            self._capabilities = reader.readString(xml)
        else:
            self._capabilities = reader.read(self.url)
        self._buildMetadata(parse_remote_metadata)


    def _buildMetadata(self, parse_remote_metadata=False):
        '''set up capabilities metadata objects: '''

        self.updateSequence = self._capabilities.attrib.get('updateSequence')

        #serviceIdentification metadata
        serviceelem=self._capabilities.find(nspath('Service'))
        self.identification=ServiceIdentification(serviceelem, self.version)

        #serviceProvider metadata
        self.provider=ServiceProvider(serviceelem)

        #serviceOperations metadata
        self.operations=[]
        for elem in self._capabilities.find(nspath('Capability/Request'))[:]:
            self.operations.append(OperationMetadata(elem))

        #serviceContents metadata: our assumption is that services use a top-level
        #layer as a metadata organizer, nothing more.

        self.contents={}
        featuretypelist=self._capabilities.find(nspath('FeatureTypeList'))
        features = self._capabilities.findall(nspath('FeatureTypeList/FeatureType'))
        for feature in features:
            cm = ContentMetadata(
                feature, featuretypelist, parse_remote_metadata, auth=self.auth)
            self.contents[cm.id]=cm

        #exceptions
        self.exceptions = [f.text for f \
                in self._capabilities.findall('Capability/Exception/Format')]

    def getcapabilities(self):
        """Request and return capabilities document from the WFS as a
        file-like object.
        NOTE: this is effectively redundant now"""
        reader = WFSCapabilitiesReader(self.version, auth=self.auth)
        return openURL(reader.capabilities_url(self.url),
                       timeout=self.timeout, auth=self.auth)

    def items(self):
        '''supports dict-like items() access'''
        items=[]
        for item in self.contents:
            items.append((item,self.contents[item]))
        return items

    def _makeStringIO(self, strval):
        """
        Helper method to make sure the StringIO being returned will work.

        Differences between Python 2.7/3.x mean we have a lot of cases to handle.

        TODO: skipped Python 2.x support. Is this still necessary?
        """
        return StringIO(strval.decode())

    def getfeature(self, typename=None, filter=None, bbox=None, featureid=None,
                   featureversion=None, propertyname='*', maxfeatures=None,
                   srsname=None, outputFormat=None, method='{http://www.opengis.net/wfs}Get',
                   startindex=None):
        """Request and return feature data as a file-like object.

        Parameters
        ----------
        typename : list
            List of typenames (string)
        filter : string
            XML-encoded OGC filter expression.
        bbox : tuple
            (left, bottom, right, top) in the feature type's coordinates.
        featureid : list
            List of unique feature ids (string)
        featureversion : string
            Default is most recent feature version.
        propertyname : list
            List of feature property names. '*' matches all.
        maxfeatures : int
            Maximum number of features to be returned.
        method : string
            Qualified name of the HTTP DCP method to use.
        srsname: string
            EPSG code to request the data in
        outputFormat: string (optional)
            Requested response format of the request.
        startindex: int (optional)
            Start position to return feature set (paging in combination with maxfeatures)


        There are 3 different modes of use

        1) typename and bbox (simple spatial query)
        2) typename and filter (more expressive)
        3) featureid (direct access to known features)
        """
        try:
            base_url = next((m.get('url') for m in self.getOperationByName('GetFeature').methods if m.get('type').lower() == method.lower()))
        except StopIteration:
            base_url = self.url
        request = {'service': 'WFS', 'version': self.version, 'request': 'GetFeature'}

        # check featureid
        if featureid:
            request['featureid'] = ','.join(featureid)
        elif bbox and typename:
            request['bbox'] = ','.join([repr(x) for x in bbox])
        elif filter and typename:
            request['filter'] = str(filter)

        if srsname:
            request['srsname'] = str(srsname)

        assert len(typename) > 0
        request['typename'] = ','.join(typename)

        if propertyname is not None:
            if not isinstance(propertyname, list):
                propertyname = [propertyname]
            request['propertyname'] = ','.join(propertyname)

        if featureversion: request['featureversion'] = str(featureversion)
        if maxfeatures: request['maxfeatures'] = str(maxfeatures)
        if startindex: request['startindex'] = str(startindex)

        if outputFormat is not None:
            request["outputFormat"] = outputFormat

        data = urlencode(request)
        log.debug("Making request: %s?%s" % (base_url, data))
        u = openURL(base_url, data, method, timeout=self.timeout, auth=self.auth)

        # check for service exceptions, rewrap, and return
        # We're going to assume that anything with a content-length > 32k
        # is data. We'll check anything smaller.

        if 'Content-Length' in u.info():
            length = int(u.info()['Content-Length'])
            have_read = False
        else:
            data = u.read()
            have_read = True
            length = len(data)

        if length < 32000:
            if not have_read:
                data = u.read()

            try:
                tree = etree.fromstring(data)
            except BaseException:
                # Not XML
                return self._makeStringIO(data)
            else:
                if tree.tag == "{%s}ServiceExceptionReport" % OGC_NAMESPACE:
                    se = tree.find(nspath('ServiceException', OGC_NAMESPACE))
                    raise ServiceException(str(se.text).strip())
                else:
                    return self._makeStringIO(data)
        else:
            if have_read:
                return self._makeStringIO(data)
            return u

    def getOperationByName(self, name):
        """Return a named content item."""
        for item in self.operations:
            if item.name == name:
                return item
        raise KeyError("No operation named %s" % name)

    def get_schema(self, typename):
        """
        Get layer schema compatible with :class:`fiona` schema object
        """

        return get_schema(self.url, typename, self.version, auth=self.auth)


class ServiceIdentification(object):
    ''' Implements IServiceIdentificationMetadata '''

    def __init__(self, infoset, version):
        self._root=infoset
        self.type = testXMLValue(self._root.find(nspath('Name')))
        self.version = version
        self.title = testXMLValue(self._root.find(nspath('Title')))
        self.abstract = testXMLValue(self._root.find(nspath('Abstract')))
        self.keywords = [f.text for f in self._root.findall(nspath('Keywords'))]
        self.fees = testXMLValue(self._root.find(nspath('Fees')))
        self.accessconstraints = testXMLValue(self._root.find(nspath('AccessConstraints')))

class ServiceProvider(object):
    ''' Implements IServiceProviderMetatdata '''
    def __init__(self, infoset):
        self._root = infoset
        self.name = testXMLValue(self._root.find(nspath('Name')))
        self.url = testXMLValue(self._root.find(nspath('OnlineResource')))
        self.keywords = extract_xml_list(self._root.find(nspath('Keywords')))

class ContentMetadata(AbstractContentMetadata):
    """Abstraction for WFS metadata.

    Implements IMetadata.
    """

    def __init__(self, elem, parent, parse_remote_metadata=False, timeout=30, auth=None):
        """."""
        super(ContentMetadata, self).__init__(auth)
        self.id = testXMLValue(elem.find(nspath('Name')))
        self.title = testXMLValue(elem.find(nspath('Title')))
        self.abstract = testXMLValue(elem.find(nspath('Abstract')))
        self.keywords = [f.text for f in elem.findall(nspath('Keywords'))]

        # bboxes
        self.boundingBox = None
        b = elem.find(nspath('LatLongBoundingBox'))
        srs = elem.find(nspath('SRS'))

        if b is not None:
            self.boundingBox = (float(b.attrib['minx']),float(b.attrib['miny']),
                    float(b.attrib['maxx']), float(b.attrib['maxy']), Crs(srs.text))

        # transform wgs84 bbox from given default bboxt
        self.boundingBoxWGS84 = None

        if b is not None and srs is not None:
            wgs84 = pyproj.Proj(init="epsg:4326")
            try:
                src_srs = pyproj.Proj(init=srs.text)
                mincorner = pyproj.transform(src_srs, wgs84, b.attrib['minx'],
                        b.attrib['miny'])
                maxcorner = pyproj.transform(src_srs, wgs84, b.attrib['maxx'],
                        b.attrib['maxy'])

                self.boundingBoxWGS84 = (mincorner[0], mincorner[1],
                        maxcorner[0], maxcorner[1])
            except RuntimeError as e:
                pass

        # crs options
        self.crsOptions = [Crs(srs.text) for srs in elem.findall(nspath('SRS'))]

        # verbs
        self.verbOptions = [op.tag for op \
            in parent.findall(nspath('Operations/*'))]
        self.verbOptions + [op.tag for op \
            in elem.findall(nspath('Operations/*')) \
            if op.tag not in self.verbOptions]

        #others not used but needed for iContentMetadata harmonisation
        self.styles=None
        self.timepositions=None
        self.defaulttimeposition=None

        # MetadataURLs
        self.metadataUrls = []
        for m in elem.findall(nspath('MetadataURL')):
            metadataUrl = {
                'type': testXMLValue(m.attrib['type'], attrib=True),
                'format': testXMLValue(m.attrib['format'], attrib=True),
                'url': testXMLValue(m)
            }
            self.metadataUrls.append(metadataUrl)

    def parse_remote_metadata(self, timeout=30):
        """Parse remote metadata for MetadataURL of format 'XML' and add it as metadataUrl['metadata']"""
        for metadataUrl in self.metadataUrls:
            if metadataUrl['url'] is not None \
                    and metadataUrl['format'].lower() == 'xml':
                try:
                    content = openURL(
                        metadataUrl['url'], timeout=timeout, auth=self.auth)
                    doc = etree.fromstring(content.read())
                    if metadataUrl['type'] == 'FGDC':
                        mdelem = doc.find('.//metadata')
                        if mdelem is not None:
                            metadataUrl['metadata'] = Metadata(mdelem)
                        else:
                            metadataUrl['metadata'] = None
                    elif metadataUrl['type'] == 'TC211':
                        mdelem = doc.find('.//' + util.nspath_eval('gmd:MD_Metadata', n.get_namespaces(['gmd']))) \
                                 or doc.find('.//' + util.nspath_eval('gmi:MI_Metadata', n.get_namespaces(['gmi'])))
                        if mdelem is not None:
                            metadataUrl['metadata'] = MD_Metadata(mdelem)
                        else:
                            metadataUrl['metadata'] = None
                except Exception:
                    metadataUrl['metadata'] = None


class OperationMetadata:
    """Abstraction for WFS metadata.

    Implements IMetadata.
    """
    def __init__(self, elem):
        """."""
        self.name = xmltag_split(elem.tag)
        # formatOptions
        self.formatOptions = [f.tag for f in elem.findall(nspath('ResultFormat/*'))]
        self.methods = []
        for verb in elem.findall(nspath('DCPType/HTTP/*')):
            url = verb.attrib['onlineResource']
            self.methods.append({'type' : xmltag_split(verb.tag), 'url': url})
