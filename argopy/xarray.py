#!/bin/env python
# -*coding: UTF-8 -*-
#

import os
import sys
import numpy as np
import xarray as xr

from argopy.errors import NetCDF4FileNotFoundError, InvalidDatasetStructure

@xr.register_dataset_accessor('argo')
class ArgoAccessor:
    """

        Class registered under scope ``argo`` to access a :class:`xarray.Dataset` object.

        # Ensure all variables are of the Argo required dtype
        ds.argo.cast_types()

        # Convert a collection of points into a collection of profiles
        ds.argo.point2profile()

        #todo

        # Convert a collection of profiles to a collection of points
        ds.argo.profile2point()

        # Make sure that the dataset complies with Argo vocabulary
        # Should be done at init with a private function ???
        # This could be usefull if a netcdf file is open directly
        ds.argo.check()



     """
    def __init__(self, xarray_obj):
        self._obj = xarray_obj
        self._added = list() # Will record all new variables added by argo
        self._dims = list(xarray_obj.dims.keys()) # Store the initial list of dimensions

        if 'N_PROF' in self._dims:
            self._type = 'profile'
        elif 'index' in self._dims:
            self._type = 'point'
        else:
            raise InvalidDatasetStructure("Argo dataset structure not recognised")

    def cast_types(self):
        """ Make sure variables are of the appropriate types

            This is hard coded, but should be retrieved from an API somewhere
        """
        if self._type != 'point':
            raise InvalidDatasetStructure("Method only available to a collection of points")
        ds = self._obj

        def cast_this(da, type):
            try:
                da.values = da.values.astype(type)
            except ValueError:
                print("Fail to cast: ", da.dtype, "into:", type)
                print("Possible values:", np.unique(da))
            return da

        for v in ds.data_vars:
            if "QC" in v:
                if ds[v].dtype == 'O': # convert object to string
                    ds[v] = cast_this(ds[v], str)

                # Address weird string values:
                # (replace missing or nan values by a '0' that will be cast as a integer later

                if ds[v].dtype == '<U3': # string, len 3 because of a 'nan' somewhere
                    ii = ds[v] == '   ' # This should not happen, but still ! That's real world data
                    ds[v].loc[dict(index=ii)] = '0'

                    ii = ds[v] == 'nan' # This should not happen, but still ! That's real world data
                    ds[v].loc[dict(index=ii)] = '0'

                    ds[v] = cast_ds(ds[v], np.dtype('U1')) # Get back to regular U1 string

                if ds[v].dtype == '<U1': # string
                    ii = ds[v] == ' ' # This should not happen, but still ! That's real world data
                    ds[v].loc[dict(index=ii)] = '0'

                # finally convert QC strings to integers:
                ds[v] = cast_this(ds[v], int)

            if v == 'PLATFORM_NUMBER' and ds['PLATFORM_NUMBER'].dtype == 'float64':  # Object
                ds['PLATFORM_NUMBER'] = cast_this(ds['PLATFORM_NUMBER'], int)

            if v == 'DATA_MODE' and ds['DATA_MODE'].dtype == 'O':  # Object
                ds['DATA_MODE'] = cast_this(ds['DATA_MODE'], str)
            if v == 'DIRECTION' and ds['DIRECTION'].dtype == 'O':  # Object
                ds['DIRECTION'] = cast_this(ds['DIRECTION'], str)
        return ds

    def point2profile(self):
        """ Transform a collection of points into a collection of profiles

        """
        if self._type != 'point':
            raise InvalidDatasetStructure("Method only available to a collection of points")
        ds = self._obj

        def fillvalue(da):
            """ Return fillvalue for a dataarray """
            # https://docs.scipy.org/doc/numpy/reference/generated/numpy.dtype.kind.html#numpy.dtype.kind
            if da.dtype.kind in ['U']:
                fillvalue = ' '
            elif da.dtype.kind == 'i':
                fillvalue = 99999
            elif da.dtype.kind == 'M':
                fillvalue = np.datetime64("NaT")
            else:
                fillvalue = np.nan
            return fillvalue

        def uid(wmo_or_uid, *cyc):
            """ UID encoder/decoder

                unique_float_profile_id = uid(690024,34) # Encode
                wmo, cyc = uid(unique_float_profile_id) # Decode
            """
            if cyc:
                return np.vectorize(int)(1e4 * wmo_or_uid + cyc).ravel()
            else:
                return np.vectorize(int)(wmo_or_uid / 1e4), -np.vectorize(int)(
                    1e4 * np.vectorize(int)(wmo_or_uid / 1e4) - wmo_or_uid)

        # Find the maximum nb of points for a single profile:
        ds['dummy_argo_counter'] = xr.DataArray(np.ones_like(ds['index'].values), dims='index',
                                                coords={'index': ds['index']})
        ds['dummy_argo_uid'] = xr.DataArray(uid(ds['PLATFORM_NUMBER'].values, ds['CYCLE_NUMBER'].values),
                                            dims='index', coords={'index': ds['index']})
        that = ds.groupby('dummy_argo_uid').sum()['dummy_argo_counter']
        N_LEVELS = int(that.max().values)
        N_PROF = len(np.unique(ds['dummy_argo_uid']))
        assert N_PROF * N_LEVELS >= len(ds['index'])

        # Create a new dataset
        # with empty ['N_PROF', 'N_LEVELS'] arrays for each variables of the dataset
        new_ds = []
        for vname in ds.data_vars:
            if ds[vname].dims == ('index',):
                new_ds.append(xr.DataArray(np.full((N_PROF, N_LEVELS), fillvalue(ds[vname]), dtype=ds[vname].dtype),
                                           dims=['N_PROF', 'N_LEVELS'],
                                           coords={'N_PROF': np.arange(N_PROF),
                                                   'N_LEVELS': np.arange(N_LEVELS)},
                                           name=vname))
        # Also add coordinates:
        for vname in ds.coords:
            if ds[vname].dims == ('index',):
                new_ds.append(xr.DataArray(np.full((N_PROF,), fillvalue(ds[vname]), dtype=ds[vname].dtype),
                                           dims=['N_PROF'],
                                           coords={'N_PROF': np.arange(0, N_PROF)},
                                           name=vname))
        new_ds = xr.merge(new_ds)
        for vname in ds.coords:
            if ds[vname].dims == ('index',):
                new_ds = new_ds.set_coords(vname)
        new_ds = new_ds.drop('index')

        # Drop N_LEVELS dims:
        vlist = ['PLATFORM_NUMBER', 'CYCLE_NUMBER']
        for vname in vlist:
            new_ds[vname] = new_ds[vname].isel(N_LEVELS=0).drop('N_LEVELS')
        # Fill in other coordinates
        vlist = ['latitude', 'longitude', 'time']
        for i_prof, dummy_argo_uid in enumerate(np.unique(ds['dummy_argo_uid'])):
            wmo, cyc = uid(dummy_argo_uid)
            new_ds['PLATFORM_NUMBER'].loc[dict(N_PROF=i_prof)] = wmo
            new_ds['CYCLE_NUMBER'].loc[dict(N_PROF=i_prof)] = cyc
            that = ds.where(ds['PLATFORM_NUMBER'] == wmo, drop=1).where(ds['CYCLE_NUMBER'] == cyc, drop=1)
            for vname in vlist:
                new_ds[vname].loc[dict(N_PROF=i_prof)] = np.unique(that[vname].values)[0]

        # Fill other variables with appropriate measurements:
        for i_prof in new_ds['N_PROF']:
            wmo = new_ds['PLATFORM_NUMBER'].sel(N_PROF=i_prof).values
            cyc = new_ds['CYCLE_NUMBER'].sel(N_PROF=i_prof).values
            that = ds.where(ds['PLATFORM_NUMBER'] == wmo, drop=1).where(ds['CYCLE_NUMBER'] == cyc, drop=1)
            N = len(that['index'])  # nb of measurements for this profile
            for vname in ds.data_vars:
                if ds[vname].dims == ('index',) and 'N_LEVELS' in new_ds[vname].dims:
                    new_ds[vname].sel(N_PROF=i_prof).loc[dict(N_LEVELS=range(0, N))] = that[vname].values

        new_ds = new_ds.drop_vars(['dummy_argo_counter', 'dummy_argo_uid'])
        new_ds = new_ds[np.sort(new_ds.data_vars)]
        new_ds.attrs = ds.attrs
        new_ds.attrs['sparsiness'] = np.round(len(ds['index']) * 100 / (N_PROF * N_LEVELS),2)

        self._type = 'profile'
        return new_ds

    def profile2point(self):
        """ Convert a collection of profiles to a collection of points """
        if self._type != 'profile':
            raise InvalidDatasetStructure("Method only available to a collection of profiles")
        ds = self._obj
        return None

class LocalLoader(object):
    """
        A generic loader class based on xarray.
        If it can't find a file, it raises a specific error for easy catching.
    """
    def __init__(self):
        self._dac = {'KM': 'kma',
                     'IF': 'coriolis',
                     'AO': 'aoml',
                     'CS': 'csiro',
                     'KO': 'kordi',
                     'JA': 'jma',
                     'HZ': 'csio',
                     'IN': 'incois',
                     'NM': 'nmdis',
                     'ME': 'meds',
                     'BO': 'bodc'}

    @staticmethod
    def _load_nc(file_path, verbose):
        """
        Loads a .nc file using xarray, with a check for file 404s.
        :param file_path:
        :return:
        """
        if os.path.isfile(file_path):
            return xr.open_dataset(file_path, decode_times=False)
        else:
            raise NetCDF4FileNotFoundError(path=file_path, verbose=verbose)

class ArgoMultiProfLocalLoader(LocalLoader):
    """
    Set the snapshot root path when you create the instance.
    Then, it knows how to navigate the folder structure of a snapshot.
    """
    def __init__(self, argo_root_path):
        LocalLoader.__init__(self)
        self.argo_root_path = argo_root_path

    def load_from_inst_code(self, institute_code, wmo, verbose=True):
        """
        Wrapper load function for argo.
        :param institute_code: the code used to identify institutes (e.g. "IF")
        :param wmo: the wmo floater ID (int)
        :param verbose: prints error message
        :return: the contents as an xrarray
        """
        doifile = os.path.join(self.argo_root_path, self._dac[institute_code], str(wmo), ("%i_prof.nc" % wmo))
        return self._load_nc(doifile, verbose=verbose)

    def load_from_inst(self, institute, wmo, verbose=True):
        """
        Wrapper load function for argo.
        :param institute: the name of the institute (e.g. "coriolis")
        :param wmo: the wmo floater ID (int)
        :param verbose: prints error message
        :return: the contents as an xrarray
        """
        doifile = os.path.join(self.argo_root_path, institute, str(wmo), ("%i_prof.nc" % wmo))
        return self._load_nc(doifile, verbose)