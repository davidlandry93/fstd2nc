from pygeode.tools import load_lib

lib = load_lib("plugins/rpn/librpn.so")


from ctypes import Structure, c_int, c_uint, c_char, c_longlong, c_ulonglong, c_float, POINTER, byref, c_void_p
Nomvar = c_char * 5
Etiket = c_char * 13
Typvar = c_char * 3


"""
typedef struct {
  int status;
  int size;
  unsigned long long int data;
  int deet;
  int npak;
  int ni;
  char grtyp;
  int nj;
  int datyp;
  int nk;
  int npas;
  int ig4;
  int ig2;
  int ig1;
  int ig3;
  Etiket etiket;
  Typvar typvar;
  Nomvar nomvar;
  int ip1;
  int ip2;
  int ip3;
  long long int dateo;
  unsigned int checksum;
} RecordHeader;
"""

class RecordHeader (Structure):
  _fields_ = [('status', c_int), ('size', c_int), ('data', c_ulonglong),
              ('deet', c_int), ('npak', c_int), ('ni', c_int),
              ('grtyp', c_char), ('nj', c_int), ('datyp', c_int),
              ('nk', c_int), ('npas', c_int), ('ig4', c_int),
              ('ig2', c_int), ('ig1', c_int), ('ig3', c_int),
              ('etiket', Etiket), ('typvar', Typvar), ('nomvar', Nomvar),
              ('ip1', c_int), ('ip2', c_int), ('ip3', c_int),
              ('dateo', c_longlong), ('checksum', c_uint)]


# What header attributes define a unique variable
unique_var_atts = 'nomvar', 'typvar', 'etiket', 'ni', 'nj', 'nk', 'grtyp', 'ip2', 'ip3', 'ig1', 'ig2', 'ig3', 'ig4'


from pygeode.var import Var
from pygeode.axis import Axis, ZAxis

# Raw axes from the file
class T(Axis): pass  # time axis
class F(Axis): pass  # forecast axis
class Z(Axis): pass  # levels (over ip1)
class K(Axis): pass  # ?? (nk)
class J(Axis): pass  # latitudes?
class I(Axis): pass  # longitudes?

# More specific sub-axes:
class Height (ZAxis): pass
class Sigma (ZAxis):
  plotorder = -1
  plotscale = 'log'

class Height_wrt_Ground (ZAxis): pass

class Theta (ZAxis): pass

# Axis with an associated type
# Slightly more specific than I or J axis, as we now have coordinate values,
#  a grid type, and associated ig1, ig2, ig3, ig4.
# We still don't interpret the values in any geophysical sense, though.
class Horizontal(Axis):
  @classmethod
  def fromvar (cls, coordvar):
    # Get the coordinate values
    values = coordvar.get().squeeze()

    # Get the metadata
    atts = dict(**coordvar.atts)
    zaxis = coordvar.getaxis('Z')
    assert len(zaxis) == 1
    atts['ip1'] = zaxis.values[0]
    faxis = coordvar.getaxis('F')
    assert len(faxis) == 1
    atts['ip2'] = faxis.values[0]

    # Instantiate
    axis = cls(values, atts=atts)
    return axis

  # Check if 2 coordinates are part of the same grid definition (matching ipX/igX)
  @staticmethod
  def compatible (x, y):
    assert isinstance(x, Horizontal)
    assert isinstance(y, Horizontal)
    for att in 'grtyp', 'ip1', 'ip2', 'ip3', 'ig1', 'ig2', 'ig3', 'ig4':
      if x.atts[att] != y.atts[att]: return False
    return True

class XCoord(Horizontal): pass  # '>>'
class YCoord(Horizontal): pass  # '^^'


del Axis, ZAxis


#kind    : level type
# 0       : height [m] (metres)
# 1       : sigma  [sg] (0.0->1.0)
# 2       : pressure [mb] (millibars)
# 3       : arbitrary code
# 4       : height [M] (metres) with respect to ground level
# 5       : hybrid coordinates [hy] (0.0->1.0)
# 6       : theta [th]


# Wrap a list of headers into a PyGeode Var
class RPN_Var (Var):
  def __init__ (self, filename, headers):
    from pygeode.var import Var
    import numpy as np

    name = headers[0].nomvar.strip()

    # Get unique time steps, forecasts, and levels
    # dateo, ip2, ip1?
    dateo_list = []
    ip2_list = []
    ip1_list = []

    for h in headers:
      if h.dateo not in dateo_list:
        dateo_list.append(h.dateo)
      if h.ip2 not in ip2_list:
        ip2_list.append(h.ip2)
      if h.ip1 not in ip1_list:
        ip1_list.append(h.ip1)

    # Encode as integers
    dateo_list = np.array(dateo_list, dtype='int64')
    ip2_list = np.array(ip2_list, dtype='int64')
    ip1_list = np.array(ip1_list, dtype='int64')

    # Define the axes
    t = T(dateo_list)
    f = F(ip2_list)
    z = Z(ip1_list)
    k = K(headers[0].nk)
    j = J(headers[0].nj)
    i = I(headers[0].ni)

    # Make sure we have all the records we need
    assert len(t) * len(f) * len(z) == len(headers), "some records are missing from variable '%s'"%name

    # Set some metadata (things that uniquely define the variable)
    atts = {}
    for att in unique_var_atts:
      atts[att] = getattr(headers[0], att)

    # Generate a matrix for the order of headers
    header_order = np.empty((len(t),len(f),len(z)), dtype=int)
    header_order[:] = -1
    for hi, h in enumerate(headers):
      ti = np.where(dateo_list == h.dateo)[0][0]
      fi = np.where(ip2_list == h.ip2)[0][0]
      zi = np.where(ip1_list == h.ip1)[0][0]
      header_order[ti,fi,zi] = hi

    self.filename = filename
    self.headers = headers
    self.header_order = header_order

    Var.__init__ (self, [t,f,z,k,j,i], name=name, atts=atts, dtype='float32')

  from pygeode.tools import need_full_axes
  @need_full_axes(I,J,K)
  def getview (self, view, pbar):
    from pygeode.tools import point
    import numpy as np
    from ctypes import c_int32, byref

    out = np.empty(view.shape, self.dtype)

    # Time, level slicing relative to *whole* variable
    TI = view.integer_indices[0]
    FI = view.integer_indices[1]
    ZI = view.integer_indices[2]

    # Generate a list of all the headers we need
    headers = []
    for ti in TI:
      for fi in FI:
        for zi in ZI:
          hi = self.header_order[ti,fi,zi]
          headers.append(self.headers[hi])
    headers = (RecordHeader*len(headers))(*headers)

    recsize = self.shape[3] * self.shape[4] * self.shape[5]
    lib.read_data (self.filename, len(headers), headers, recsize, point(out))

    pbar.update(100)

    return out
  del need_full_axes

del Var

#TODO - remove this
# deprecated - this has been split into several smaller, logical steps.
# Higher-level wrapper for vars
def wrap (var, stuff):
  from pygeode.axis import Axis, Lat, Lon, gausslat, Pres, ZAxis, Hybrid, TAxis
  from pygeode.timeaxis import StandardTime
  import numpy as np
  from warnings import warn

  # Skip coordinate variables
  if var.name in ('>>','^^','!!','HY'): return None

  # Time axis
  nt = int(var.var_.nt)

  dateo = np.array(var.var_.t[:nt])
  dateo = dateo/4 * 5
  # Case 0: degenerate time axis
  if np.all(dateo == 0):
    warn ("degenerate time axis detected", stacklevel=3)
    taxis = TAxis(list(dateo))
  # Case 1: old style
  elif np.all(dateo < 123200000):
    dateo /= 10;  # ignore operation run digit
    hour = dateo % 100; dateo /= 100
    year = 1900 + (dateo % 100); dateo /= 100
    day = dateo % 100; dateo /= 100
    month = dateo
    badmonths = (month < 1) + (month > 12)
    if np.any(badmonths):
      warn("Invalid months detected.  Resetting to 1.", stacklevel=3)
      month[badmonths] = 1
    baddays = (day < 1) + (day > 31)
    if np.any(baddays):
      warn("Invalid days detected.  Resetting to 1.", stacklevel=3)
      day[baddays] = 1
    taxis = StandardTime (year=year, month=month, day=day, units='hours')
  # Case 2: new style
  else:
    dateo -= 123200000;
    dateo *= 4;  # now have # seconds since Jan 1, 1980
    # Convert to hours
    dateo /= 3600.
    taxis = StandardTime (values=dateo, units='hours', startdate={'year':1980})

  # Forecast axis
  #TODO: figure out why we can't use 'Axis' here
  faxis = F(var.var_.ip2[:var.var_.nforecasts], name='forecast')

  ###

  # Horizontal axes
  grtyp = var.var_.grtyp
  ig1 = var.var_.ig1
  ig2 = var.var_.ig2
  ig3 = var.var_.ig3
  ig4 = var.var_.ig4
  ni = int(var.var_.ni)
  nj = int(var.var_.nj)
  if grtyp == 'A':  # lat-lon
    xaxis = 360./ni * np.arange(ni)
    yaxis = -90 + 180./nj * (np.arange(nj)+0.5) #1/2 gridpoint offset

  elif grtyp == 'B':  # lat-lon, with poles
    xaxis = 360./(ni-1) * np.arange(ni)
    yaxis = -90 + 180./(nj-1) * np.arange(nj) - 90


  elif grtyp == 'G':  # gaussian grid
    assert ig1 == 0, "can only handle global gaussian grid right now"
    xaxis = 360./ni * np.arange(ni)
    yaxis = gausslat(nj).values

  elif grtyp == 'Z':  # other lat-lon mesh?
    # Get matching coords
    coords = []
    for v in stuff:
      if v.name not in ('>>','^^'): continue
      V = v.var_
      if V.ip1[0] != ig1 or V.ip2[0] != ig2 or V.ip3 != ig3:
        continue
      if v.name == '>>' and V.ni != ni: continue
      if v.name == '^^' and V.nj != nj: continue
      coords.append(v)
    xaxis = [v for v in coords if v.name == '>>']
    if len(xaxis) != 1:
      print "error - %s - len(xaxis) = %d != 1"%(var.name,len(xaxis))
      return None
    xaxis = xaxis[0]
    yaxis = [v for v in coords if v.name == '^^']
    if len(yaxis) != 1:
      print "error - %s - len(yaxis) = %d != 1"%(var.name,len(yaxis))
      return None
    yaxis = yaxis[0]

    #TODO: check for rotated grids (gridtyp = 'E' and IG1/IG2/IG3/IG4 indicate rotation?
    if xaxis.var_.grtyp == 'E':
      xaxis = Lon(xaxis.squeeze().get())
      yaxis = Lat(yaxis.squeeze().get())
    else:
      xaxis = XCoord.fromvar(xaxis)
      yaxis = YCoord.fromvar(yaxis)

  else:
    print "unhandled grid type '%s' for variable '%s'"%(grtyp,var.name)
    return None  # ignore this variable?

  # Adjust the order of the latitudes?
  if grtyp in ('A','B','G'):
    # Global?
    if ig1 == 0: pass
    # Northern Hemisphere?
    elif ig1 == 1:
      yaxis = (yaxis+90)/2
    # Southern Hemisphere?
    elif ig1 == 2:
      yaxis = (yaxis-90)/2

    # South to north?
    if ig2 == 0: pass
    # North to south?
    elif ig2 == 1: yaxis = yaxis[::-1]

    # Convert the X/Y coords to Lat/Lon
    xaxis = Lon(xaxis)
    yaxis = Lat(yaxis)

  #TODO

  # Vertical axis
  #kind    : level type
  # 0       : height [m] (metres)
  # 1       : sigma  [sg] (0.0->1.0)
  # 2       : pressure [mb] (millibars)
  # 3       : arbitrary code
  # 4       : height [M] (metres) with respect to ground level
  # 5       : hybrid coordinates [hy] (0.0->1.0)
  # 6       : theta [th]

#// units of ip1
#char ip1units[][3] = {"m", "sg", "mb", "", "M", "hy", "th"};

#  ip1kind = var.var_.ip1kind
  ip1 = np.array(var.var_.ip1[:var.var_.nz])
  # Get the kind of level
  ip1kind = ip1>>24
  if len(set(ip1kind)) != 1:
    warn ("incompatible level types found in the same var for '%s'"%var.name,stacklevel=2)
    return None
  ip1kind = set(ip1kind).pop()
  zclass = {0:Height, 1:Sigma, 2:Pres, 3:ZAxis, 4:Height_wrt_Ground, 5:Hybrid, 6:Theta}[ip1kind]

  # Convert codes to integer values
  ip1 &= 0x0FFFFFF;
  exp = ip1>>20;
  ip1 &= 0x00FFFFF;
  zaxis = np.asarray(ip1,'float32')
  zaxis *= 10000;
#  while (exp > 0) { h->ip1float /= 10; exp--; }
  zaxis /= 10.**exp
  
  if zclass is Hybrid:
    hy = [v for v in stuff if v.name == 'HY' and v.var_.etiket == var.var_.etiket]
    if len(hy) != 1:
      print 'error - %s - len(hy) = %d != 1'%(var.name,len(hy))
      return None
    hy = hy[0]
    p0 = hy.var_.ig1 * 100.
    assert hy.var_.nz == 1
#    plid = hy.var_.z[0] * 100.
    plid = zaxis[0] * 100.
    exp = hy.var_.ig2 / 1000.
    etatop = plid / p0
    zaxis = np.array(zaxis)
    zaxis[np.where(zaxis<etatop)] = etatop  # fix bug where zaxis is slightly less than etatop
    B = ((zaxis - etatop)/(1-etatop))**exp
    A = p0 * (zaxis - B)
    zaxis = Hybrid(zaxis,A,B)
  else:
    zaxis = zclass(zaxis)


  #TODO

  newvar = var.replace_axes (t=taxis, f=faxis, z=zaxis, i=xaxis, j=yaxis)

  remove_axes = []
  # Remove k dimension?
  if var.var_.nk == 1: remove_axes.append(3)
  # Remove levels (if only level is 0m above ground)
  if type(zaxis) == Height and len(zaxis) == 1 and zaxis.values == [0]:
    remove_axes.append(2)
#  # Remove forecast axis if there is no forecast
#  if len(faxis) == 1 and faxis.values == [0]:
#    remove_axes.append(1)
  # Remove degenerate time axis?
  if len(taxis) == 1 and not isinstance(taxis,StandardTime) and taxis.values == [0]:
    remove_axes.append(0)

  if len(remove_axes) > 0: newvar = newvar.squeeze(*remove_axes)

  return newvar

# Helper method - group headers based on what variable the represent
def collect_headers (headers):
  var_headers = {}
  for h in headers:
    # Generate a unique key for this variable
    key = tuple(getattr(h,att) for att in unique_var_atts)
    if key not in var_headers:
      var_headers[key] = []
    # Add this header to the variable
    var_headers[key].append(h)

  return var_headers
  
# Helper method - check if a coordinate is compatible with a variable
# (on raw variables)
def uses_coord (v, coord):
  if v.atts['ig1'] != coord.atts['ip1']: return False
  if v.atts['ig2'] != coord.atts['ip2']: return False
  if v.atts['ig3'] != coord.atts['ip3']: return False
  return True

# Helper method - apply the coordinate variables ('>>','^^', 'HY') to the variables
def attach_xycoords (varlist):

  # Get the various coords
  xcoords = [XCoord.fromvar(v) for v in varlist if v.name == '>>']
  ycoords = [YCoord.fromvar(v) for v in varlist if v.name == '^^']

  varlist = [v for v in varlist if v.name not in ('>>','^^')]

  # Loop over the other (non-coordinate) variables, and apply whatever
  # coords we can.
  for i,v in enumerate(varlist):
    icoord = v.getaxis(I)
    jcoord = v.getaxis(J)
    for xcoord in xcoords:
      if uses_coord (v, xcoord): icoord = xcoord
    for ycoord in ycoords:
      if uses_coord (v, ycoord): jcoord = ycoord

    varlist[i] = v.replace_axes(I = icoord, J = jcoord)

  return varlist


def open (filename):
  from pygeode.dataset import Dataset
  from os.path import exists

  assert exists(filename), "Can't find %s"%filename

  nrecs = lib.get_num_records(filename)
  print nrecs

  headers = (RecordHeader*nrecs)()

  lib.get_record_headers (filename, headers)

  vars = collect_headers(headers)

  # Generate PyGeode vars based on the header information
  vars = [RPN_Var(filename, headers) for headers in vars.values()]
  print vars

  # Do some filtering
  vars = attach_xycoords(vars)
  print vars

  # Convert to a dataset
  dataset = Dataset(vars)

  print dataset

  return dataset


# Miscellaneous functions
# (could be moved to a utility library)

# Polar stereographic conversion.
# Create a 2D array of latitudes and longitudes from the given 1D arrays of coordinates

# based on some random code found at
# http://www.windatlas.ca/scripts/xy_ll.js
# which was probably based on the LLFXY function in the RPN libraries?
def llfxy (x,y,d60,dgrw,nhem):
  import numpy as np

  # Reshape x and y into 2D arrays
  X = x.reshape(-1,1)
  Y = y.reshape(1,-1)

  c_b3 = 90.
  c_b4 = 180.
  rdtodg=180. / np.pi
  re=1.866025*6371000/d60
  re2=re*re

  dlat=90.
  dlon=0.

  dlon = np.arctan2(Y, X) * rdtodg

#  if x < 0:
#    dlon += r_sign(c_b4, y)
  dlon[x<0,:] += 180. * np.sign(Y)

  dlon -= dgrw

#  if dlon > 180.:
#    dlon += -360.
  dlon[dlon>180] -= 360.

#  if dlon < -180.:
#    dlon += 360.
  dlon[dlon<-180] += 360.

  r2 = X*X + Y*Y
  dlat = (re2-r2) / (re2+r2)
  dlat = np.arcsin(dlat) * rdtodg

  if nhem == 2:
    dlat = -(dlat)
    dlon = -(dlon)


  return dlat, dlon

# Filter function
# Check for any XCoord/YCoord axes, generate latitude and longitude fields
def add_latlon (dataset):
  from pygeode.var import Var
  from pygeode.dataset import Dataset

  # Pull out the variables
  vars = list(dataset.vars)

  # Loop over all XCoords
  for xaxis in dataset.axes:
    if not isinstance(xaxis,XCoord): continue
    # Find the matching YCoord
    for yaxis in dataset.axes:
      if not isinstance(yaxis,YCoord): continue
      if not Horizontal.compatible(xaxis,yaxis): continue

      grtyp = xaxis.atts['grtyp']

      if grtyp in ('N', 'S'):
        d60, dgrw = map(xaxis.atts.get, ['ig3', 'ig4'])
        nhem = 1 if grtyp == 'N' else 2
        lat, lon = llfxy (xaxis.values, yaxis.values, d60, dgrw, nhem)
      else:
        print "add_latlon: can't handle grtyp '%s' yet"%grtyp
        continue

      # Convert the raw lat/lon values to a Var
      lat = Var([xaxis,yaxis], values=lat, name='latitudes')
      lon = Var([xaxis,yaxis], values=lon, name='longitudes')

      # Append these to the list of vars
      vars.append(lat)
      vars.append(lon)

  # Regroup the variables together
  dataset = Dataset(vars, atts=dataset.atts)

  return dataset
