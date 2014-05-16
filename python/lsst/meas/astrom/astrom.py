import os
import math

import lsst.daf.base as dafBase
import lsst.pex.logging as pexLog
import lsst.pex.exceptions as pexExceptions
import lsst.pex.config as pexConfig
import lsst.afw.geom as afwGeom
import lsst.afw.table as afwTable
import lsst.afw.image as afwImage
import lsst.meas.algorithms.utils as maUtils

from .config import MeasAstromConfig, AstrometryNetDataConfig
import sip as astromSip

import numpy as np # for isfinite()

# Object returned by determineWcs.
class InitialAstrometry(object):
    '''
    getWcs(): sipWcs or tanWcs
    getMatches(): sipMatches or tanMatches

    Other fields are:
    solveQa (PropertyList)
    tanWcs (Wcs)
    tanMatches (MatchList)
    sipWcs (Wcs)
    sipMatches (MatchList)
    matchMetadata (PropertyList)
    '''
    def __init__(self):
        self.tanWcs = None
        self.tanMatches = None
        self.sipWcs = None
        self.sipMatches = None
        self.matchMetadata = dafBase.PropertyList()
        self.solveQa = None

    def getMatches(self):
        '''
        Get "sipMatches" -- MatchList using the SIP WCS solution, if it
        exists, or "tanMatches" -- MatchList using the TAN WCS solution
        otherwise.
        '''
        return self.sipMatches or self.tanMatches

    def getWcs(self):
        '''
        Returns the SIP WCS, if one was found, or a TAN WCS
        '''
        return self.sipWcs or self.tanWcs

    matches = property(getMatches)
    wcs = property(getWcs)
    
    ### "Not very pythonic!" complains Paul.
    # Consider these methods deprecated; if you want these elements, just
    # .grab them.
    def getSipWcs(self):
        return self.sipWcs
    def getTanWcs(self):
        return self.tanWcs
    def getSipMatches(self):
        return self.sipMatches
    def getTanMatches(self):
        return self.tanMatches
    def getMatchMetadata(self):
        return self.matchMetadata
    def getSolveQaMetadata(self):
        return self.solveQa
    
    
class Astrometry(object):
    ConfigClass = MeasAstromConfig

    '''
    About Astrometry.net index files (astrometry_net_data): 
    '''

    '''
    Note about memory management of astrometry_net index files:
    index_t* structs (which include memory maps and other resources)
    are loaded either from single index files (listed in the
    AstrometryNetDataConfig entry "indexFiles", or from multiindex
    files (listed in "multiIndexFiles").  Singles are stored in .sinds,
    multis in .minds.  These are loaded in _readIndexFiles(), called
    from the constructor, and freed in _closeIndexFiles(), called from 
    the __del__ destructor.
    '''


    def __init__(self,
                 config,
                 andConfig=None,
                 log=None,
                 logLevel=pexLog.Log.INFO):
        '''
        conf: an AstromConfig object.
        andConfig: an AstromNetDataConfig object
        log: a pexLogging.Log
        logLevel: if log is None, the log level to use
        '''
        self.config = config
        if log is not None:
            self.log = log
        else:
            self.log = pexLog.Log(pexLog.Log.getDefaultLog(),
                                  'meas.astrom',
                                  logLevel)
        if andConfig is None:
            # ASSUME SETUP IN EUPS
            dirnm = os.environ.get('ASTROMETRY_NET_DATA_DIR')
            if dirnm is None:
                raise RuntimeError("astrometry_net_data is not setup")

            andConfig = AstrometryNetDataConfig()
            fn = os.path.join(dirnm, 'andConfig.py')
            if not os.path.exists(fn):
                raise RuntimeError('astrometry_net_data config file \"%s\" required but not found' % fn)
            andConfig.load(fn)

        self.andConfig = andConfig
        self._readIndexFiles()

    # RAII

    ## FIXME -- push this into SWIG multiindex_t ?
    class _OpenMultiindexFile(object):
        def __init__(self, fn):
            import astrometry_net as an
            mi = an.multiindex_new(fn)
            if mi is None:
                raise RuntimeError('Failed to read stars from multiindex filename "%s"' % fn)
            self.mind = mi
        def __del__(self):
            print 'Closing & freeing multiindex file'
            import astrometry_net as an
            an.multiindex_close(self.mind)
            self.mind = None

    class _LoadMultiindexFile(object):
        def __init__(self, mind):
            import astrometry_net as an
            self.mind = mind
            print 'Loading multiindex file'
            an.multiindex_reload_starkd(self.mind)
        def __del__(self):
            import astrometry_net as an
            print 'Unloading multiindex file'
            an.multiindex_unload(self.mind)
            self.mind = None

    def _readIndexFiles(self):
        import astrometry_net as an
        # .minds: multi-index objects
        self.minds = []

        # merge indexFiles and multiIndexFiles; we'll treat both as
        # multiindex for simplicity.
        mindfiles = ([(True,[fn,fn]) for fn  in self.andConfig.indexFiles] +
                     [(False,fns)    for fns in self.andConfig.multiIndexFiles])

        for single,fns in mindfiles:
            # First filename in "fns" is star kdtree, the rest are index files.
            fn = fns[0]
            if single:
                self.log.log(self.log.DEBUG, 'Adding index file %s' % fns[0])
            else:
                self.log.log(self.log.DEBUG, 'Adding multiindex files %s' % str(fns))
            fn2 = self._getIndexPath(fn)
            if fn2 is None:
                if single:
                    self.log.logdebug('Unable to find index file %s' % fn)
                else:
                    self.log.logdebug('Unable to find star part of multiindex file %s' % fn)
                nMissing += 1
                continue
            fn = fn2
            self.log.log(self.log.DEBUG, 'Path: %s' % fn)

            omi = Astrometry._OpenMultiindexFile(fn)
            for i,fn in enumerate(fns[1:]):
                self.log.log(self.log.DEBUG, 'Reading index from multiindex file "%s"' % fn)
                fn2 = self._getIndexPath(fn)
                if fn2 is None:
                    self.log.logdebug('Unable to find index part of multiindex file %s' % fn)
                    nMissing += 1
                    continue
                fn = fn2
                self.log.log(self.log.DEBUG, 'Path: %s' % fn)
                if an.multiindex_add_index(omi.mind, fn, an.INDEX_ONLY_LOAD_METADATA):
                    raise RuntimeError('Failed to read index from multiindex filename "%s"' % fn)
                ind = omi.mind.getIndex(0)
                self.log.log(self.log.DEBUG, '  index %i, hp %i (nside %i), nstars %i, nquads %i' %
                                (ind.indexid, ind.healpix, ind.hpnside, ind.nstars, ind.nquads))
            an.multiindex_unload_starkd(omi.mind)
            self.minds.append(omi)

        if not self.inds:
            self.log.warn('Unable to find any index files')
        elif nMissing > 0:
            self.log.warn('Unable to find %d index files' % nMissing)

    def _debug(self, s):
        self.log.log(self.log.DEBUG, s)
    def _warn(self, s):
        self.log.log(self.log.WARN, s)

    def setAndConfig(self, andconfig):
        self.andConfig = andconfig

    def _getImageParams(self, wcs, exposure, filterName=None, imageSize=None,
                        x0=None, y0=None):
        if exposure is not None:
            ex0,ey0 = exposure.getX0(), exposure.getY0()
            if x0 is None:
                x0 = ex0
            if y0 is None:
                y0 = ey0
            self._debug('Got exposure x0,y0 = %i,%i' % (ex0,ey0))
            if filterName is None:
                filterName = exposure.getFilter().getName()
                self._debug('Setting filterName = "%s" from exposure metadata' % str(filterName))
            if imageSize is None:
                imageSize = (exposure.getWidth(), exposure.getHeight())
                self._debug('Setting image size = (%i, %i) from exposure metadata' % (imageSize))
        if x0 is None:
            x0 = 0
        if y0 is None:
            y0 = 0
        return filterName, imageSize, x0, y0

    def useKnownWcs(self, sources, wcs=None, exposure=None, filterName=None, imageSize=None, x0=None, y0=None):
        """
        Returns an InitialAstrometry object, just like determineWcs,
        but assuming the given input WCS is correct.

        This is enabled by the pipe_tasks AstrometryConfig
        'forceKnownWcs' option.  If you are using that option, you
        probably also want to turn OFF 'calculateSip'.

        This involves searching for reference sources within the WCS
        area, and matching them to the given 'sources'.  If
        'calculateSip' is set, we will try to compute a TAN-SIP
        distortion correction.

        sources: list of detected sources in this image.
        wcs: your known WCS
        exposure: the exposure holding metadata for this image.
        filterName: string, filter name, eg "i"
        x0,y0: image origin / offset; these coordinates along with the
           "imageSize" determine the bounding-box in pixel coordinates of
           the image in question; this is used for finding reference sources
           in the image, among other things.
        
        You MUST provide a WCS, either by providing the 'wcs' kwarg
        (an lsst.image.Wcs object), or by providing the 'exposure' on
        which we will call 'exposure.getWcs()'.

        You MUST provide a filter name, either by providing the
        'filterName' kwarg (a string), or by setting the 'exposure';
        we will call 'exposure.getFilter().getName()'.

        You MUST provide the image size, either by providing the
        'imageSize' kwargs, an (W,H) tuple of ints, or by providing
        the 'exposure'; we will call 'exposure.getWidth()' and
        'exposure.getHeight()'.

        Note, when modifying this function, that it is also called by
        'determineWcs' (via 'determineWcs2'), since the steps are all
        the same.
        """
        # return value:
        astrom = InitialAstrometry()

        if wcs is None:
            if exposure is None:
                raise RuntimeError('useKnownWcs: need either "wcs=" or "exposure=" kwarg.')
            wcs = exposure.getWcs()
            if wcs is None:
                raise RuntimeError('useKnownWcs: wcs==None and exposure.getWcs()==None.')
                
        filterName,imageSize,x0,y0 = self._getImageParams(exposure=exposure, wcs=wcs,
                                                          imageSize=imageSize,
                                                          filterName=filterName,
                                                          x0=x0, y0=y0)
        pixelMargin = 50.

        cat = self.getReferenceSourcesForWcs(wcs, imageSize, filterName, pixelMargin, x0=x0, y0=y0)
        catids = [src.getId() for src in cat]
        uids = set(catids)
        self.log.logdebug('%i reference sources; %i unique IDs' % (len(catids), len(uids)))
        matchList = self._getMatchList(sources, cat, wcs)
        uniq = set([sm.second.getId() for sm in matchList])
        if len(matchList) != len(uniq):
            self._warn(('The list of matched stars contains duplicate reference source IDs ' +
                        '(%i sources, %i unique ids)') % (len(matchList), len(uniq)))
        if len(matchList) == 0:
            self._warn('No matches found between input sources and reference catalogue.')
            return astrom

        self._debug('%i reference objects match input sources using input WCS' % (len(matchList)))
        astrom.tanMatches = matchList
        astrom.tanWcs = wcs
        
        srcids = [s.getId() for s in sources]
        for m in matchList:
            assert(m.second.getId() in srcids)
            assert(m.second in sources)

        if self.config.calculateSip:
            sipwcs,matchList = self._calculateSipTerms(wcs, cat, sources, matchList, imageSize, x0=x0, y0=y0)
            if sipwcs == wcs:
                self._debug('Failed to find a SIP WCS better than the initial one.')
            else:
                self._debug('%i reference objects match input sources using SIP WCS' % (len(matchList)))
                astrom.sipWcs = sipwcs
                astrom.sipMatches = matchList
                
        W,H = imageSize
        wcs = astrom.getWcs()
        # _getMatchList() modifies the source list RA,Dec coordinates.
        # Here, we make them consistent with the WCS we are returning.
        for src in sources:
            src.updateCoord(wcs)
        astrom.matchMetadata = _createMetadata(W, H, x0, y0, wcs, filterName)
        return astrom

    def determineWcs(self,
                     sources,
                     exposure,
                     **kwargs):
        """
        Finds a WCS solution for the given 'sources' in the given
        'exposure', getting other parameters from config.

        Valid kwargs include:

        'radecCenter', an afw.coord.Coord giving the RA,Dec position
           of the center of the field.  This is used to limit the
           search done by Astrometry.net (to make it faster and load
           fewer index files, thereby using less memory).  Defaults to
           the RA,Dec center from the exposure's WCS; turn that off
           with the boolean kwarg 'useRaDecCenter' or config option
           'useWcsRaDecCenter'

        'useRaDecCenter', a boolean.  Don't use the RA,Dec center from
           the exposure's initial WCS.

        'searchRadius', in degrees, to search for a solution around
           the given 'radecCenter'; default from config option
           'raDecSearchRadius'.

        'useParity': parity is the 'flip' of the image.  Knowing it
           reduces the search space (hence time) for Astrometry.net.
           The parity can be computed from the exposure's WCS (the
           sign of the determinant of the CD matrix); this option
           controls whether we do that or force Astrometry.net to
           search both parities.  Default from config.useWcsParity.

        'pixelScale': afwGeom.Angle, estimate of the angle-per-pixel
           (ie, arcseconds per pixel).  Defaults to a value derived
           from the exposure's WCS.  If enabled, this value, plus or
           minus config.pixelScaleUncertainty, will be used to limit
           Astrometry.net's search.

        'usePixelScale': boolean.  Use the pixel scale to limit
           Astrometry.net's search?  Defaults to config.useWcsPixelScale.

        'filterName', a string, the filter name of this image.  Will
           be mapped through the 'filterMap' config dictionary to a
           column name in the astrometry_net_data index FITS files.
           Defaults to the exposure.getFilter() value.

        'imageSize', a tuple (W,H) of integers, the image size.
           Defaults to the exposure.get{Width,Height}() values.

        """
        assert(exposure is not None)

        margs = kwargs.copy()
        if not 'searchRadius' in margs:
            margs.update(searchRadius = self.config.raDecSearchRadius * afwGeom.degrees)
        if not 'usePixelScale' in margs:
            margs.update(usePixelScale = self.config.useWcsPixelScale)
        if not 'useRaDecCenter' in margs:
            margs.update(useRaDecCenter = self.config.useWcsRaDecCenter)
        if not 'useParity' in margs:
            margs.update(useParity = self.config.useWcsParity)
        margs.update(exposure=exposure)
        return self.determineWcs2(sources, **margs)

    def determineWcs2(self, sources, **kwargs):
        '''
        Get a blind astrometric solution for the given list of sources.

        We need:
          -the image size;
          -the filter

        And if available, we can use:
          -an initial Wcs estimate;
             --> RA,Dec center
             --> pixel scale
             --> "parity"
             
        (all of which are metadata of Exposure).

        filterName: string
        imageSize: (W,H) integer tuple/iterable
        pixelScale: afwGeom::Angle per pixel.
        radecCenter: afwCoord::Coord
        '''
        wcs,qa = self.getBlindWcsSolution(sources, **kwargs)
        kw = {}
        # Keys passed to useKnownWcs
        for key in ['exposure', 'filterName', 'imageSize', 'x0', 'y0']:
            if key in kwargs:
                kw[key] = kwargs[key]
        astrom = self.useKnownWcs(sources, wcs=wcs, **kw)
        astrom.solveQa = qa
        astrom.tanWcs = wcs
        return astrom

    def getBlindWcsSolution(self, sources, 
                            exposure=None,
                            wcs=None,
                            imageSize=None,
                            x0=None, y0=None,
                            radecCenter=None,
                            searchRadius=None,
                            pixelScale=None,
                            filterName=None,
                            doTrim=False,
                            usePixelScale=True,
                            useRaDecCenter=True,
                            useParity=True,
                            searchRadiusScale=2.):
        if not useRaDecCenter and radecCenter is not None:
            raise RuntimeError('radecCenter is set, but useRaDecCenter is False.  Make up your mind!')
        if not usePixelScale and pixelScale is not None:
            raise RuntimeError('pixelScale is set, but usePixelScale is False.  Make up your mind!')

        filterName,imageSize,x0,y0 = self._getImageParams(exposure=exposure, wcs=wcs,
                                                          imageSize=imageSize,
                                                          filterName=filterName,
                                                          x0=x0, y0=y0)

        if exposure is not None:
            if wcs is None:
                wcs = exposure.getWcs()
                self._debug('Setting initial WCS estimate from exposure metadata')

        if imageSize is None:
            # Could guess from the extent of the Sources...
            raise RuntimeError('Image size must be specified by passing "exposure" or "imageSize"')

        W,H = imageSize
        xc, yc = W/2. + 0.5 + x0, H/2. + 0.5 + y0
        parity = None

        if wcs is not None:
            if pixelScale is None:
                if usePixelScale:
                    pixelScale = wcs.pixelScale()
                    self._debug('Setting pixel scale estimate = %.3f from given WCS estimate' %
                                (pixelScale.asArcseconds()))

            if radecCenter is None:
                if useRaDecCenter:
                    radecCenter = wcs.pixelToSky(xc, yc)
                    self._debug(('Setting RA,Dec center estimate = (%.3f, %.3f) from given WCS '
                                 + 'estimate, using pixel center = (%.1f, %.1f)') %
                                (radecCenter.getLongitude().asDegrees(),
                                 radecCenter.getLatitude().asDegrees(), xc, yc))

            if searchRadius is None:
                if useRaDecCenter:
                    assert(pixelScale is not None)
                    searchRadius = (pixelScale * math.hypot(W,H)/2. *
                                    searchRadiusScale)
                    self._debug(('Using RA,Dec search radius = %.3f deg, from pixel scale, '
                                 + 'image size, and searchRadiusScale = %g') %
                                (searchRadius, searchRadiusScale))
            if useParity:
                parity = wcs.isFlipped()
                self._debug('Using parity = %s' % (parity and 'True' or 'False'))

        if doTrim:
            n = len(sources)
            if exposure is not None:
                bbox = afwGeom.Box2D(exposure.getMaskedImage().getBBox(afwImage.PARENT))
            else:
                # CHECK -- half-pixel issues here?
                bbox = afwGeom.Box2D(afwGeom.Point2D(0.,0.), afwGeom.Point2D(W, H))
            sources = _trimBadPoints(sources, bbox)
            self._debug("Trimming: kept %i of %i sources" % (n, len(sources)))

        wcs,qa = self._solve(sources, wcs, imageSize, pixelScale, radecCenter, searchRadius, parity,
                             filterName, xy0=(x0,y0))
        if wcs is None:
            raise RuntimeError("Unable to match sources with catalog.")
        self.log.info('Got astrometric solution from Astrometry.net')

        rdc = wcs.pixelToSky(xc, yc)
        self._debug('New WCS says image center pixel (%.1f, %.1f) -> RA,Dec (%.3f, %.3f)' %
                    (xc, yc, rdc.getLongitude().asDegrees(), rdc.getLatitude().asDegrees()))
        return wcs,qa

    def getSipWcsFromWcs(self, wcs, imageSize, x0=0, y0=0, ngrid=20,
                         linearizeAtCenter=True):
        '''
        This function allows one to get a TAN-SIP WCS, starting from
        an existing WCS.  It uses your WCS to compute a fake grid of
        corresponding "stars" in pixel and sky coords, and feeds that
        to the regular SIP code.

        linearizeCenter: if True, get a linear approximation of the input
          WCS at the image center and use that as the TAN initialization for
          the TAN-SIP solution.  You probably want this if your WCS has its
          CRPIX outside the image bounding box.
          
        '''
        # Ugh, build src and ref tables
        srcSchema = afwTable.SourceTable.makeMinimalSchema()
        key = srcSchema.addField("centroid", type="PointD")
        srcTable = afwTable.SourceTable.make(srcSchema)
        srcTable.defineCentroid("centroid")
        srcs = srcTable
        refs = afwTable.SimpleTable.make(afwTable.SimpleTable.makeMinimalSchema())
        cref = []
        csrc = []
        (W,H) = imageSize
        for xx in np.linspace(0., W, ngrid):
            for yy in np.linspace(0, H, ngrid):
                src = srcs.makeRecord()
                src.set(key.getX(), x0 + xx)
                src.set(key.getY(), y0 + yy)
                csrc.append(src)
                rd = wcs.pixelToSky(afwGeom.Point2D(xx + x0, yy + y0))
                ref = refs.makeRecord()
                ref.setCoord(rd)
                cref.append(ref)

        if linearizeAtCenter:
            # Linearize the original WCS around the image center to create a
            # TAN WCS.
            # Reference pixel in LSST coords
            crpix = afwGeom.Point2D(x0 + W/2. - 0.5, y0 + H/2. - 0.5)
            crval = wcs.pixelToSky(crpix)
            crval = crval.getPosition(afwGeom.degrees)
            # Linearize *AT* crval to get effective CD at crval.
            # (we use the default skyUnit of degrees as per WCS standard)
            aff = wcs.linearizePixelToSky(crval)
            cd = aff.getLinear().getMatrix()
            wcs = afwImage.Wcs(crval, crpix, cd)
                
        return self.getSipWcsFromCorrespondences(wcs, cref, csrc, (W,H),
                                                 x0=x0, y0=y0)

    
    def getSipWcsFromCorrespondences(self, origWcs, cat, sources, imageSize,
                                     x0=0, y0=0):
        '''
        Produces a SIP solution given a list of known correspondences.
        Unlike _calculateSipTerms, this does not iterate the solution;
        it assumes you have given it a good sets of corresponding stars.

        NOTE that "cat" and "sources" are assumed to be the same length;
        entries "cat[i]" and "sources[i]" are assumed to be correspondences.

        origWcs: the WCS to linearize in order to get the TAN part of the
           TAN-SIP WCS.

        cat: reference source catalog

        sources: image sources

        imageSize, x0, y0: these describe the bounding-box of the image,
            which is used when computing reverse SIP polynomials.

        '''
        sipOrder = self.config.sipOrder
        bbox = afwGeom.Box2I(afwGeom.Point2I(x0,y0),
                             afwGeom.Extent2I(imageSize[0], imageSize[1]))
        matchList = []
        for ci,si in zip(cat, sources):
            matchList.append(afwTable.ReferenceMatch(ci, si, 0.))

        sipObject = astromSip.makeCreateWcsWithSip(matchList, origWcs, sipOrder, bbox)
        return sipObject.getNewWcs()
    
    def _calculateSipTerms(self, origWcs, cat, sources, matchList, imageSize,
                           x0=0, y0=0):
        '''
        Iteratively calculate SIP distortions and regenerate matchList based on improved WCS.

        origWcs: original WCS object, probably (but not necessarily) a TAN WCS;
           this is used to set the baseline when determining whether a SIP
           solution is any better; it will be returned if no better SIP solution
           can be found.

        matchList: list of supposedly matched sources, using the "origWcs".

        cat: reference source catalog

        sources: sources in the image to be solved

        imageSize, x0, y0: these determine the bounding-box of the image,
           which is used when finding reverse SIP coefficients.
        '''
        sipOrder = self.config.sipOrder
        wcs = origWcs
        bbox = afwGeom.Box2I(afwGeom.Point2I(x0,y0),
                             afwGeom.Extent2I(imageSize[0], imageSize[1]))

        i=0
        lastScatPix = None
        while True:
            try:
                sipObject = astromSip.makeCreateWcsWithSip(matchList, wcs, sipOrder, bbox)
                if lastScatPix is None:
                    lastScatPix = sipObject.getLinearScatterInPixels()
                proposedWcs = sipObject.getNewWcs()
                scatPix = sipObject.getScatterInPixels()
                self.plotSolution(matchList, proposedWcs, imageSize)
            except pexExceptions.LsstCppException, e:
                self._warn('Failed to calculate distortion terms. Error: ' + str(e))
                break

            matchSize = len(matchList)
            # use new WCS to get new matchlist.
            proposedMatchlist = self._getMatchList(sources, cat, proposedWcs)

            self._debug('SIP iteration %i: %i objects match.  Median scatter is %g arcsec = %g pixels (vs previous: %i matches, %g pixels)' %
                        (i, len(proposedMatchlist), sipObject.getScatterOnSky().asArcseconds(), scatPix, matchSize, lastScatPix))
            #self._debug('Proposed WCS: ' + proposedWcs.getFitsMetadata().toString())
            # Hack convergence tests
            if len(proposedMatchlist) < matchSize:
                break
            if len(proposedMatchlist) == matchSize and scatPix >= lastScatPix:
                break

            wcs = proposedWcs
            matchList = proposedMatchlist
            lastScatPix = scatPix
            matchSize = len(matchList)
            i += 1

        return wcs, matchList

    def plotSolution(self, matchList, wcs, imageSize):
        """Plot the solution, when debugging is turned on.

        @param matchList   The list of matches
        @param wcs         The Wcs
        @param imageSize   2-tuple with the image size (W,H)
        """
        import lsstDebug
        display = lsstDebug.Info(__name__).display 
        if not display:
            return

        try:
            import matplotlib.pyplot as plt
            import numpy
        except ImportError:
            print >> sys.stderr, "Unable to import matplotlib: %s" % e
            return

        fig = plt.figure(1)
        fig.clf()
        try:
            fig.canvas._tkcanvas._root().lift() # == Tk's raise, but raise is a python reserved word
        except:                                 # protect against API changes
            pass

        num = len(matchList)
        x = numpy.zeros(num)
        y = numpy.zeros(num)
        dx = numpy.zeros(num)
        dy = numpy.zeros(num)
        for i, m in enumerate(matchList):
            x[i] = m.second.getX()
            y[i] = m.second.getY()
            pixel = wcs.skyToPixel(m.first.getCoord())
            dx[i] = x[i] - pixel.getX()
            dy[i] = y[i] - pixel.getY()

        subplots = maUtils.makeSubplots(fig, 2, 2, xgutter=0.1, ygutter=0.1, pygutter=0.04)

        def plotNext(x, y, xLabel, yLabel, xMax):
            ax = subplots.next()
            ax.set_autoscalex_on(False)
            ax.set_xbound(lower=0, upper=xMax)
            ax.scatter(x, y)
            ax.set_xlabel(xLabel)
            ax.set_ylabel(yLabel)
            ax.axhline(0.0)

        plotNext(x, dx, "x", "dx", imageSize[0])
        plotNext(x, dy, "x", "dy", imageSize[0])
        plotNext(y, dx, "y", "dx", imageSize[1])
        plotNext(y, dy, "y", "dy", imageSize[1])

        fig.show()

        while True:
            try:
                reply = raw_input("Pausing for inspection, enter to continue... [hpQ] ").strip()
            except EOFError:
                reply = "n"

            reply = reply.split()
            if reply:
                reply, args = reply[0], reply[1:]
            else:
                reply = ""

            if reply in ("", "h", "p", "Q"):
                if reply == "h":
                    print "h[elp] p[db] Q[uit]"
                    continue
                elif reply == "p":
                    import pdb; pdb.set_trace() 
                elif reply == "Q":
                    sys.exit(1)
                break

    def _getMatchList(self, sources, cat, wcs):
        dist = self.config.catalogMatchDist * afwGeom.arcseconds
        clean = self.config.cleaningParameter
        matcher = astromSip.MatchSrcToCatalogue(cat, sources, wcs, dist)
        matchList = matcher.getMatches()
        if matchList is None:
            # Produce debugging stats...
            X = [src.getX() for src in sources]
            Y = [src.getY() for src in sources]
            R1 = [src.getRa().asDegrees() for src in sources]
            D1 = [src.getDec().asDegrees() for src in sources]
            R2 = [src.getRa().asDegrees() for src in cat]
            D2 = [src.getDec().asDegrees() for src in cat]
            # for src in sources:
            #self._debug("source: x,y (%.1f, %.1f), RA,Dec (%.3f, %.3f)" %
            #(src.getX(), src.getY(), src.getRa().asDegrees(), src.getDec().asDegrees()))
            #for src in cat:
            #self._debug("ref: RA,Dec (%.3f, %.3f)" %
            #(src.getRa().asDegrees(), src.getDec().asDegrees()))
            self.loginfo('_getMatchList: %i sources, %i reference sources' % (len(sources), len(cat)))
            if len(sources):
                self.loginfo('Source range: x [%.1f, %.1f], y [%.1f, %.1f], RA [%.3f, %.3f], Dec [%.3f, %.3f]' %
                             (min(X), max(X), min(Y), max(Y), min(R1), max(R1), min(D1), max(D1)))
            if len(cat):
                self.loginfo('Reference range: RA [%.3f, %.3f], Dec [%.3f, %.3f]' %
                             (min(R2), max(R2), min(D2), max(D2)))
            raise RuntimeError('No matches found between image and catalogue')
        matchList = astromSip.cleanBadPoints.clean(matchList, wcs, nsigma=clean)
        return matchList

    def getColumnName(self, filterName, columnMap, default=None):
        '''
        Returns the column name in the astrometry_net_data index file that will be used
        for the given filter name.

        @param filterName   Name of filter used in exposure
        @param columnMap    Dict that maps filter names to column names
        @param default      Default column name
        '''
        filterName = self.config.filterMap.get(filterName, filterName) # Exposure filter --> desired filter
        try:
            return columnMap[filterName] # Desired filter --> a_n_d column name
        except KeyError:
            self.log.warn("No column in configuration for filter '%s'; using default '%s'" %
                          (filterName, default))
            return default

    def getCatalogFilterName(self, filterName):
        """Deprecated method for retrieving the magnitude column name from the filter name"""
        return self.getColumnName(filterName, self.andConfig.magColumnMap, self.andConfig.defaultMagColumn)


    def getReferenceSourcesForWcs(self, wcs, imageSize, filterName, pixelMargin=50, x0=0, y0=0, trim=True):
        W,H = imageSize
        xc, yc = W/2. + 0.5, H/2. + 0.5
        rdc = wcs.pixelToSky(x0 + xc, y0 + yc)
        ra,dec = rdc.getLongitude(), rdc.getLatitude()
        self._debug('Getting reference sources using center: pixel (%.1f, %.1f) -> RA,Dec (%.3f, %.3f)' %
                    (xc, yc, ra.asDegrees(), dec.asDegrees()))
        pixelScale = wcs.pixelScale()
        rad = pixelScale * (math.hypot(W,H)/2. + pixelMargin)
        self._debug('Getting reference sources using radius of %.3g deg' % rad.asDegrees())
        cat = self.getReferenceSources(ra, dec, rad, filterName)
        # NOTE: reference objects don't have (x,y) anymore, so we can't apply WCS to set x,y positions
        if trim:
            # cut to image bounds + margin.
            bbox = afwGeom.Box2D(afwGeom.Point2D(x0, y0), afwGeom.Extent2D(W, H))
            bbox.grow(pixelMargin)
            cat = self._trimBadPoints(cat, bbox, wcs=wcs) # passing wcs says to compute x,y on-the-fly
        return cat


    def getReferenceSources(self, ra, dec, radius, filterName):
        '''
        Searches for reference-catalog sources (in the
        astrometry_net_data files) in the requested RA,Dec region
        (afwGeom::Angle objects), with the requested radius (also an
        Angle).  The flux values will be set based on the requested
        filter (None => default filter).
        
        Returns: an lsst.afw.table.SimpleCatalog of reference objects
        '''
        sgCol = self.andConfig.starGalaxyColumn
        varCol = self.andConfig.variableColumn
        idcolumn = self.andConfig.idColumn

        magCol = self.getColumnName(filterName, self.andConfig.magColumnMap, self.andConfig.defaultMagColumn)
        magerrCol = self.getColumnName(filterName, self.andConfig.magErrorColumnMap,
                                       self.andConfig.defaultMagErrorColumn)

        if self.config.allFluxes:
            names = []
            mcols = []
            ecols = []
            if magCol:
                names.append('flux')
                mcols.append(magCol)
                ecols.append(magerrCol)

            for col,mcol in self.andConfig.magColumnMap.items():
                names.append(col)
                mcols.append(mcol)
                ecols.append(self.andConfig.magErrorColumnMap.get(col, ''))
            margs = (names, mcols, ecols)

        else:
            margs = (magCol, magerrCol)

        '''
        Note about multiple astrometry_net index files and duplicate IDs:

        -as of astrometry_net 0.30, we take a reference catalog and build
         a set of astrometry_net index files from it, with each one covering a
         region of sky and a range of angular scales.  The index files covering
         the same region of sky at different scales use exactly the same stars.
         Therefore, if we search every index file, we will get multiple copies of
         each reference star (one from each index file).
         For now, we have the "unique_ids" option to solver.getCatalog().
         -recall that the index files to be used are specified in the
          AstrometryNetDataConfig.indexFiles flat list.

        -as of astrometry_net 0.40, we have the capability to share
         the reference stars between index files (called
         "multiindex"), so we will no longer have to repeat the
         reference stars in each index.  We will, however, have to
         change the way the index files are configured to take
         advantage of this functionality.  Once this is in place, we
         can eliminate the horrid ID checking and deduplication (in solver.getCatalog()).
        '''

        solver = self._getSolver()

        # Find multi-index files within range
        radecrad = (ra.asDegrees(), dec.asDegrees(), radius.asDegrees())
        minds = self._getMIndexesWithinRange(*radecrad)

        # Load the mindex files within range
        loaded = [Astrometry._LoadMultiindexFile(omi.mind) for omi in minds]

        # We just want to pass the star kd-trees, so just pass the
        # first element of each multi-index.
        inds = [omi.mind.getIndex(0) for omi in minds]

        cat = solver.getCatalog(*((inds,) + radecrad + (idcolumn,)
                                  + margs + (sgCol, varCol)))
        # Unload
        del loaded
        del solver
        return cat

    def _solve(self, sources, wcs, imageSize, pixelScale, radecCenter,
               searchRadius, parity, filterName=None, xy0=None):
        solver = self._getSolver()

        x0,y0 = 0,0
        if xy0 is not None:
            x0,y0 = xy0

        # select sources with valid x,y, flux
        xybb = afwGeom.Box2D()
        goodsources = afwTable.SourceCatalog(sources.table)
        for s in sources:
            if np.isfinite(s.getX()) and np.isfinite(s.getY()) and np.isfinite(s.getPsfFlux()):
                goodsources.append(s)
                xybb.include(afwGeom.Point2D(s.getX() - x0, s.getY() - y0))
        if len(goodsources) < len(sources):
            self.log.logdebug('Keeping %i of %i sources with finite X,Y positions and PSF flux' %
                              (len(goodsources), len(sources)))
        self._debug(('Feeding sources in range x=[%.1f, %.1f], y=[%.1f, %.1f] ' +
                     '(after subtracting x0,y0 = %.1f,%.1f) to Astrometry.net') %
                    (xybb.getMinX(), xybb.getMaxX(), xybb.getMinY(), xybb.getMaxY(), x0, y0))
        # setStars sorts them by PSF flux.
        solver.setStars(goodsources, x0, y0)
        solver.setMaxStars(self.config.maxStars)
        solver.setImageSize(*imageSize)
        solver.setMatchThreshold(self.config.matchThreshold)
        radecrad = None
        if radecCenter is not None:
            radecrad = (radecCenter.getLongitude().asDegrees(),
                        radecCenter.getLatitude().asDegrees(),
                        searchRadius.asDegrees())
            solver.setRaDecRadius(*radecrad)
            self.log.logdebug('Searching for match around RA,Dec = (%g, %g) with radius %g deg' %
                              radecrad)

        if pixelScale is not None:
            dscale = self.config.pixelScaleUncertainty
            scale = pixelScale.asArcseconds()
            lo = scale / dscale
            hi = scale * dscale
            solver.setPixelScaleRange(lo, hi)
            self.log.logdebug('Searching for matches with pixel scale = %g +- %g %% -> range [%g, %g] arcsec/pix' %
                              (scale, 100.*(dscale-1.), lo, hi))

        if parity is not None:
            solver.setParity(parity)
            self.log.logdebug('Searching for match with parity = ' + str(parity))

        # Find and load index files within RA,Dec range and scale range.
        if radecrad is not None:
            minds = self._getMIndexesWithinRange(*radecrad)
        else:
            minds = self.minds
        #qlo,qhi = solver.getQuadSizeRangeArcsec()
        qlo,qhi = solver.getQuadSizeLow(), solver.getQuadSizeHigh()
        print 'Quad size range', qlo,qhi
        loaded = []
        ntotal = sum([omi.mind.nIndices() for omi in self.minds])
        active = []
        for omi in minds:
            mi = omi.mind
            loadedmi = False
            for i in range(mi.nIndices()):
                ind = mi.getIndex(i)
                if not ind.overlapsScaleRange(qlo, qhi):
                    continue
                if not loadedmi:
                    loaded.append(Astrometry._LoadMultiindexFile(mi))
                    loadedmi = True
                if ind.reload():
                    raise RuntimeError('Failed to reload index file %s' % ind.indexname)
                active.append(ind.indexname)
                solver.addIndex(ind)

        self.log.logdebug('Searching for match in %i of %i index files: [ %s ]' %
                          (len(active), ntotal, ', '.join(active)))
        cpulimit = self.config.maxCpuTime
        solver.run(cpulimit)

        # unload index files
        del loaded

        if solver.didSolve():
            self.log.logdebug('Solved!')
            wcs = solver.getWcs()
            self.log.logdebug('WCS: %s' % wcs.getFitsMetadata().toString())

            if x0 != 0 or y0 != 0:
                wcs.shiftReferencePixel(x0, y0)
                self.log.logdebug('After shifting reference pixel by x0,y0 = (%i,%i), WCS is: %s' %
                                  (x0, y0, wcs.getFitsMetadata().toString()))

        else:
            self.log.warn('Did not get an astrometric solution from Astrometry.net')
            wcs = None
            # Gather debugging info...

            # -are there any reference stars in the proposed search area?
            if radecCenter is not None:
                ra = radecCenter.getLongitude()
                dec = radecCenter.getLatitude()
                refs = self.getReferenceSources(ra, dec, searchRadius, filterName)
                self.log.info('Searching around RA,Dec = (%g,%g) with radius %g deg yields %i reference-catalog sources' %
                              (ra.asDegrees(), dec.asDegrees(), searchRadius.asDegrees(), len(refs)))

        qa = solver.getSolveStats()
        self.log.logdebug('qa: %s' % qa.toString())
        return wcs, qa

    def _getIndexPath(self, fn):
        if os.path.isabs(fn):
            return fn
        andir = os.getenv('ASTROMETRY_NET_DATA_DIR')
        if andir is not None:
            fn2 = os.path.join(andir, fn)
            if os.path.exists(fn2):
                return fn2

        if os.path.exists(fn):
            return os.path.abspath(fn)
        else:
            return None

    def _getMIndexesWithinRange(self, ra, dec, radius):
        '''
        ra,dec,radius: [deg], spatial cut based on the healpix of the index

        Returns list of multiindex objects within range.
        (actually, _OpenMultiindexFile objects)
        '''
        good = []
        for omi in self.minds:
            if omi.mind.isWithinRange(ra, dec, radius):
                good.append(omi)
        return good

    def _getSolver(self):
        import astrometry_net as an
        solver = an.solver_new()
        # HACK, set huge default pixel scale range.
        lo,hi = 0.01, 3600.
        solver.setPixelScaleRange(lo, hi)
        return solver

    @staticmethod
    def _trimBadPoints(sources, bbox, wcs=None):
        '''Remove elements from catalog whose xy positions are not within the given bbox.

        sources:  a Catalog of SimpleRecord or SourceRecord objects
        bbox: an afwImage.Box2D
        wcs:  if not None, will be used to compute the xy positions on-the-fly;
              this is required when sources actually contains SimpleRecords.
        
        Returns:
        a list of Source objects with xAstrom,yAstrom within the bbox.
        '''
        keep = type(sources)(sources.table)
        for s in sources:
            point = s.getCentroid() if wcs is None else wcs.skyToPixel(s.getCoord())
            if bbox.contains(point):
                keep.append(s)
        return keep

    def joinMatchListWithCatalog(self, packedMatches, sourceCat):
        '''
        This function is required to reconstitute a ReferenceMatchVector after being
        unpersisted.  The persisted form of a ReferenceMatchVector is the 
        normalized Catalog of IDs produced by afw.table.packMatches(), with the result of 
        InitialAstrometry.getMatchMetadata() in the associated tables\' metadata.

        The "live" form of a matchlist has links to
        the real record objects that are matched; it is "denormalized".
        This function takes a normalized match catalog, along with the catalog of
        sources to which the match catalog refers.  It fetches the reference
        sources that are within range, and then denormalizes the matches
        -- sets the "matchList[*].first" and "matchList[*].second" entries
        to point to the sources in the "sources" argument, and to the
        reference sources fetched from the astrometry_net_data files.
    
        @param[in] packedMatches  Unpersisted match list (an lsst.afw.table.BaseCatalog).
                                  packedMatches.table.getMetadata() must contain the
                                  values from InitialAstrometry.getMatchMetadata()
        @param[in,out] sourceCat  Source catalog used for the 'second' side of the matches
                                  (an lsst.afw.table.SourceCatalog).  As a side effect,
                                  the catalog will be sorted by ID.
        
        @return An lsst.afw.table.ReferenceMatchVector of denormalized matches.
        '''
        matchmeta = packedMatches.table.getMetadata()
        version = matchmeta.getInt('SMATCHV')
        if version != 1:
            raise ValueError('SourceMatchVector version number is %i, not 1.' % version)
        filterName = matchmeta.getString('FILTER').strip()
        # all in deg.
        ra = matchmeta.getDouble('RA') * afwGeom.degrees
        dec = matchmeta.getDouble('DEC') * afwGeom.degrees
        rad = matchmeta.getDouble('RADIUS') * afwGeom.degrees
        self.log.logdebug('Searching RA,Dec %.3f,%.3f, radius %.1f arcsec, filter "%s"' %
                          (ra.asDegrees(), dec.asDegrees(), rad.asArcseconds(), filterName))
        refCat = self.getReferenceSources(ra, dec, rad, filterName)
        self.log.logdebug('Found %i reference catalog sources in range' % len(refCat))
        refCat.sort()
        sourceCat.sort()
        return afwTable.unpackMatches(packedMatches, refCat, sourceCat)


def _createMetadata(width, height, x0, y0, wcs, filterName):
    """
    Create match metadata entries required for regenerating the catalog

    @param width Width of the image (pixels)
    @param height Height of the image (pixels)
    @param x0 x offset of image origin from parent (pixels)
    @param y0 y offset of image origin from parent (pixels)
    @param filterName Name of filter, used for magnitudes
    @return Metadata
    """
    meta = dafBase.PropertyList()

    # cache: field center and size.
    cx,cy = x0 + 0.5 + width/2., y0 + 0.5 + height/2.
    radec = wcs.pixelToSky(cx, cy).toIcrs()
    meta.add('RA', radec.getRa().asDegrees(), 'field center in degrees')
    meta.add('DEC', radec.getDec().asDegrees(), 'field center in degrees')
    imgSize = wcs.pixelScale() * math.hypot(width, height)/2.
    meta.add('RADIUS', imgSize.asDegrees(),
             'field radius in degrees, approximate')
    meta.add('SMATCHV', 1, 'SourceMatchVector version number')
    if filterName is not None:
        meta.add('FILTER', filterName, 'LSST filter name for tagalong data')
    return meta

def readMatches(butler, dataId, sourcesName='icSrc', matchesName='icMatch', config=MeasAstromConfig(),
                sourcesFlags=afwTable.SOURCE_IO_NO_FOOTPRINTS):
    """Read matches, sources and catalogue; combine.

    @param butler Data butler
    @param dataId Data identifier for butler
    @param sourcesName Name for sources from butler
    @param matchesName Name for matches from butler
    @param sourcesFlags Flags to pass for source retrieval
    @returns Matches
    """
    sources = butler.get(sourcesName, dataId, flags=sourcesFlags)
    packedMatches = butler.get(matchesName, dataId)
    astrom = Astrometry(config)
    return astrom.joinMatchListWithCatalog(packedMatches, sources)
