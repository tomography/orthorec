import sys
import dxchange
import h5py
import numpy as np
import cupy as cp
from cupyx.scipy.fft import rfft, irfft
import concurrent.futures
import kernels
import signal
import os
from utils import tic, toc


def backprojection(data, theta, center, idx, idy, idz):
    """Compute backprojection to orthogonal slices"""
    [nz, n] = data.shape[1:]
    obj = cp.zeros([len(center), n, 3*n], dtype='float32')
    obj[:, :nz, :n] = kernels.orthox(data, theta, center, idx)
    obj[:, :nz, n:2*n] = kernels.orthoy(data, theta, center, idy)
    obj[:, :n, 2*n:3*n] = kernels.orthoz(data, theta, center, idz)
    return obj


def fbp_filter(data):
    """FBP filtering of projections"""
    t = cp.fft.rfftfreq(data.shape[2])
    wfilter = t * (1 - t * 2)**3  # parzen
    wfilter = cp.tile(wfilter, [data.shape[1], 1])
    # loop over slices to minimize fft memory overhead
    for k in range(data.shape[0]):
        data[k] = irfft(
            wfilter*rfft(data[k], overwrite_x=True, axis=1), overwrite_x=True, axis=1)
    return data


def darkflat_correction(data, dark, flat):
    """Dark-flat field correction"""
    for k in range(data.shape[0]):
        data[k] = (data[k]-dark)/cp.maximum(flat-dark, 1e-6)
    return data


def minus_log(data):
    """Taking negative logarithm"""
    data = -cp.log(cp.maximum(data, 1e-6))
    return data


def fix_inf_nan(data):
    """Fix inf and nan values in projections"""
    data[cp.isnan(data)] = 0
    data[cp.isinf(data)] = 0
    return data


def binning(data, bin_level):
    for k in range(bin_level):
        data = 0.5*(data[..., ::2, :]+data[..., 1::2, :])
        data = 0.5*(data[..., :, ::2]+data[..., :, 1::2])
    return data


def gpu_copy(data, theta, start, end, bin_level):
    data_gpu = cp.array(data[start:end]).astype('float32')
    theta_gpu = cp.array(theta[start:end]).astype('float32')
    data_gpu = binning(data_gpu, bin_level)
    return data_gpu, theta_gpu


def recon(data, dark, flat, theta, center, idx, idy, idz):
    data = darkflat_correction(data, dark, flat)
    data = minus_log(data)
    data = fix_inf_nan(data)
    data = fbp_filter(data)
    obj = backprojection(data, theta*cp.pi/180.0, center, idx, idy, idz)
    return obj


def orthorec(fin, center, idx, idy, idz, bin_level):

    # projection chunk size to fit data to gpu memory
    # e.g., data size is (1500,2048,2448), pchunk=100 gives splitting data into chunks (100,2048,2448)
    # that are processed sequentially by one GPU
    pchunk = 64  # fine for gpus with >=8GB memory
    # change pars wrt binning
    idx //= pow(2, bin_level)
    idy //= pow(2, bin_level)
    idz //= pow(2, bin_level)
    center /= pow(2, bin_level)

    # init range of centers
    center = cp.arange(center-20, center+20, 0.5).astype('float32')

    print('Try centers:', center)

    # init pointers to dataset in the h5 file
    fid = h5py.File(fin, 'r')
    data = fid['exchange/data']
    flat = fid['exchange/data_white']
    dark = fid['exchange/data_dark']
    theta = fid['exchange/theta']
    ang = np.pi/12
    # compute mean of dark and flat fields on GPU
    dark_gpu = cp.mean(cp.array(dark), axis=0).astype('float32')
    flat_gpu = cp.median(cp.array(flat), axis=0).astype('float32')
    dark_gpu = binning(dark_gpu, bin_level)
    flat_gpu = binning(flat_gpu, bin_level)
    print('1. Read data from memory')
    tic()
    data = data[:]
    theta = theta[:]
    #ids = np.where((theta%np.pi>ang)*(theta%np.pi<np.pi-ang))[0]
    #print(len(ids))
    #data = data[ids]    
    #theta = theta[ids]
    
    print('Time:', toc())

    print('2. Reconstruction of orthoslices')
    tic()
    # recover x,y,z orthoslices by projection chunks, merge them in one image
    # reconstruction pipeline consists of 2 threads for processing and for cpu-gpu data transfer
    obj_gpu = cp.zeros([len(center), data.shape[2]//pow(2, bin_level),
                        3*data.shape[2]//pow(2, bin_level)], dtype='float32')
    nchunk = int(cp.ceil(data.shape[0]/pchunk))
    data_gpu = [None]*2
    theta_gpu = [None]*2
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        for k in range(0, nchunk+1):
            # thread for cpu-gpu copy
            if(k < nchunk):
                t2 = executor.submit(
                    gpu_copy, data, theta, k*pchunk, min((k+1)*pchunk, data.shape[0]), bin_level)
            # thread for processing
            if(k > 1):
                t3 = executor.submit(recon, data_gpu[(
                    k-1) % 2], dark_gpu, flat_gpu, theta_gpu[(k-1) % 2], center, idx, idy, idz)

            # gather results from 2 threads
            if(k < nchunk):
                data_gpu[k % 2], theta_gpu[k % 2] = t2.result()
            if(k > 1):
                obj_gpu += t3.result()

    obj_gpu /= data.shape[0]
    print('Time:', toc())
    # save result as tiff
    print('3. Cpu-gpu copy and save reconstructed orthoslices')
    tic()
    
    obj = obj_gpu.get()
    for i in range(len(center)):
        foutc = "%s_rec/vn/try_rec/%s/bin%d/r_%.2f" % (os.path.dirname(fin),os.path.basename(fin)[:-3], bin_level, center[i])
        print(foutc)
        dxchange.write_tiff(obj[i], foutc, overwrite=True)
    print('Time:', toc())

    cp._default_memory_pool.free_all_blocks()


def signal_handler(sig, frame):
    """Calls abort_scan when ^C is typed"""
    cp._default_memory_pool.free_all_blocks()
    print('Abort')
    exit()


if __name__ == "__main__":
    """Recover x,y,z ortho slices on GPU
    Parameters
    ----------
    fin : str
        Input h5 file.
    center : float
        Rotation center
    idx,idy,idz : int
        x,y,z ids of ortho slices
    bin_level: int
        binning level

    Example of execution:        
    python orthorec.py /local/data/423_coal5wtNaBr5p.h5 1224 512 512 512 1
    """
    # Set ^C interrupt to abort the scan
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTSTP, signal_handler)

    fin = sys.argv[1]
    center = cp.float32(sys.argv[2])
    idx = cp.int32(sys.argv[3])
    idy = cp.int32(sys.argv[4])
    idz = cp.int32(sys.argv[5])
    bin_level = cp.int32(sys.argv[6])

    orthorec(fin, center, idx, idy, idz, bin_level)
