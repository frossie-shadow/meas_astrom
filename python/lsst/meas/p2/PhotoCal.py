
import os    
import pdb
import math
import numpy as np

import eups
import lsst.meas.astrom as measAst
import lsst.meas.astrom.sip as sip
import sourceSetIO as ssi
import astNet

#plot=False
#if plot:
    #import matplotlib.pyplot as mpl

def prep():
    filename = os.path.join(eups.productDir("meas_astrom"), "examples", "cfht.xy.txt")
    srcSet = ssi.read(filename)
    
    gas = astNet.loadAstromSolver()
    flag=astNet.solveSrcSet(gas, srcSet, numObject=50, raDec=[334.303012, -17.233988], plateScaleLwr=.18,
                        plateScaleUpr=.19)
    
    if flag==False:
        print "No solution found"
        return
        
    wcs = gas.getWcs()
    imgSizeInArcsec = measAst.getImageSizeInArcsec(srcSet, wcs)
    cat = gas.getCatalogue(float(2*imgSizeInArcsec)) #Catalogue of nearby stars

    #Now generate a list of matching objects
    distInArcsec = 1
    cleanParam = 3

    matchList = measAst.matchSrcAndCatalogue(cat=cat, img=srcSet, wcs=wcs, 
        distInArcsec=distInArcsec, cleanParam=cleanParam)
            
    if len(matchList) == 0:
        print "No matches found between input source and catalogue."
        print "Something in wrong. Defaulting to input wcs"
        return 
    
    return matchList
        


    

def calcPhotoCal(sourceMatch, log=None):
    """Calculate photometric calibration, i.e the zero point magnitude"""
    
    #Convert fluxes to magnitudes
    catMags, instMags = getMagnitudes(sourceMatch)

    #Fit to get zeropoint
    lsf = robustFit(catMags, instMags, order=2, plot=True)
    par = lsf.getParams()
    err = lsf.getErrors()
    
    #Sanity check output
    if not(par[1] - err[1] < 1 and par[1] + err[1] > 1):
        raise RuntimeError("Slope of fitting function is not 1 (%g +- %g) " %(par[1], err[1]))
        
    return float(par[0]), float(err[0])


def getMagnitudes(sourceMatch):
    
    #Extract the fluxes as numpy arrays
    fluxCat = np.array(map(lambda x: x.first.getPsfFlux(), sourceMatch))
    fluxSrc = np.array(map(lambda x: x.second.getPsfFlux(), sourceMatch))
    
    #I don't think I want errors
    fluxCatErr = np.array(map(lambda x: x.first.getPsfFluxErr(), sourceMatch))
    fluxSrcErr = np.array(map(lambda x: x.second.getPsfFluxErr(), sourceMatch))
    
    #Remove bad values
    fluxSrc[ fluxSrc<=0 ] = 1e-99
    fluxCat[ fluxCat<=0 ] = 1e-99

    #@DEBUG. Catalogue fluxes are currently set to zero, so we'll invent values for them
    fluxCat = fluxSrc*.251
    
    #Convert to mags
    magSrc = -2.5*np.log10(fluxSrc)
    magCat = -2.5*np.log10(fluxCat)
    
    #magSrcErr = fluxSrcErr/fluxSrc/np.log(10)
    #magCatErr = fluxSrcErr/fluxSrc/np.log(10)
    
    return magSrc, magCat
    


def robustFit(x, y, order=2, plot=False):
    """\brief Fit a polynomial to a dataset in a manner that is highly insensitive to outliers
    
    Proceedure is to bin the data into order+1 points. The x value of the bin is the mean x value
    of the points in the bin, and the y value of the bin is the *median* of the y values of the 
    points in the bin. This approach is very resistant to outliers affecting the fit.
    
    Input
    \param x       Array of ordinate values to fit
    \param y       Array of co-ordinate values
    \param order=2 Order of fit. Default (2) means to fit a straight line 
    """
    
    if len(x) == 0:
        raise ValueError("Input x array has zero length")
    
    if len(x) != len(y):
        raise ValueError("Input x and y arrays are of different length")
        
    if order <= 0:
        raise ValueError("Order must be >=1")
        
    if order > len(x)/3:
        #Hard to discriminate against outliers with only two points per bin
        raise ValueError("Order can be no greater than one third the number of data points")
        
    nBins = order+1
    idx = x.argsort()   #indices of the sorted array of x
    
    rx = chooseRobustX(x, idx, nBins)
    ry = chooseRobustY(y, idx, nBins)
    rs = np.ones(nBins)


    #if plot:
        #mpl.plot(x, y, 'ro')
        #mpl.plot(rx, ry, 'ks-')
        
    return sip.LeastSqFitter1dPoly(list(rx), list(ry), list(rs), order)
    


def chooseRobustX(x, idx, nBins):
    """\brief Create nBins values of the ordinate based on the mean of groups of elements of x
    
    Inputs:
    \param x Ordinate to be binned
    \param idx Indices of x in sorted order, i.e x[idx[i]] <= x[idx[i+1]]
    \param nBins Number of desired bins
    """

    if len(x) == 0:
        raise ValueError("x array has no data")
        
    if len(x) != len(idx):
        raise ValueError("Length of x and idx don't agree")
        
    if nBins < 1:
        raise ValueError("nBins < 1")
        
    rSize = len(idx)/float(nBins)  #Note, a floating point number
    rx = np.zeros(nBins)
    
    for i in range(nBins):
        rng = range(int(rSize*i), int(rSize*(i+1)))
        rx[i] = np.median(x[idx[rng]])
    return rx
    


def chooseRobustY(y, idx, nBins):
    """\brief Create nBins values of the ordinate based on the mean of groups of elements of x
    
    Inputs:
    \param y Co-ordinate to be binned
    \param idx Indices of y in sorted order, i.e y[idx[i]] <= y[idx[i+1]]
    \param nBins Number of desired bins
    """

    if len(y) == 0:
        raise ValueError("y array has no data")
        
    if len(y) != len(idx):
        raise ValueError("Length of y and idx don't agree")
        
    if nBins < 1:
        raise ValueError("nBins < 1")

    rSize = len(idx)/float(nBins)  #Note, a floating point number
    ry = np.zeros(nBins)
    
    for i in range(nBins):
        rng = range(int(rSize*i), int(rSize*(i+1)))
        ry[i] = np.median(y[idx[rng]])
    return ry



def clean(x, y, order=2, sigmaClip=3, maxIter=5):
    """\brief Remove outliers from the set of {(x,y)}
    
    Robust-fits a polynomial to y(x) and remove points that like more than sigmaClip times
    the scatter away from the line. Repeat until no more points are removed, or maxIter is reached
    The arrays x and y are modified.
    
    \param x ordinate array (numpy array)
    \param y coordinate array (numpy array)
    \param order  Order of polynomial to fit
    \param sigmaClip. Remove points more this number times the variance from the fit
    \param maxIter  Maximum number of iterations
    """
    
    if len(x) == 0:
        raise ValueError("Input x array has zero length")
    
    if len(x) != len(y):
        raise ValueError("Input x and y arrays are of different length")
        
    if order <= 0:
        raise ValueError("Order must be >=1")
        
    if order > len(x)/3:
        #Hard to discriminate against outliers with only two points per bin
        raise ValueError("Order can be no greater than one third the number of data points")

    if sigmaClip<0:
        raise ValueError("sigmaClip must be >=1")
        
    if maxIter<0:
        raise ValueError("maxIter must be >=1")
        
    i=0
    newSize = len(x)
    oldSize = newSize+1
    while newSize < oldSize and i<maxIter:
        lsf = robustFit(x,y,order)
        f = map(lambda x: lsf.valueAt(float(x)), x)
        
        
        sigma = (y-f).std()
        if sigma == 0:  #A perfect fit. Something odd with the data, but not our concern
            break
            
        deviance = np.fabs( (y - f) /sigma)
        idx = np.where(deviance < sigmaClip)
        pdb.set_trace()
        x=x[idx]
        y=y[idx]
        
        oldSize=newSize
        newSize = len(x)
        










def main():
    sourceMatch = prep()
    print calcPhotometricZeroPoint(sourceMatch)  
    if plot:
        mpl.show()

