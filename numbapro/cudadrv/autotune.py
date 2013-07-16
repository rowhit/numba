'''
- Parse jit compile info
- Compute warp occupany histogram
'''
from __future__ import division
import math
import re


#------------------------------------------------------------------------------
# autotuning

def _cmp_occupancy(a, b):
    '''prefer greater occupancy but less thread-per-block
    '''
    ao, at = a
    bo, bt = b
    if ao < bo:
        return -1
    elif ao > bo:
        return 1
    else:
        return -1 * at.__cmp__(bt)


class AutoTuner(object):
    '''Autotune a kernel based upon the theoretical occupancy.
    '''

    def __init__(self, name, compile_info, cc):
        '''
        :param name: kernel name
        :param compile_info: kernel compile log as generated by ptxas
        :param cc: compute capability as a tuple-2 ints
        '''
        allinfo = parse_compile_info(compile_info)
        info = allinfo[name]
        self.table = warp_occupancy(info, cc=cc)

        self.cc = cc
        self.by_occupancy = list(reversed(sorted(((occup, tpb)
                                                    for tpb, (occup, factor)
                                                    in self.table.iteritems()),
                                                 cmp=_cmp_occupancy)))

    def best(self):
        return self.max_occupancy_max_blocks()

    def max_occupancy_max_blocks(self):
        '''Returns the thread-per-block that optimizes for 
        maximum occupancy and maximum blocks.
        
        Maximum blocks allows for the best utilization of parallel execution
        because each block can be executed concurrently on different SM.
        '''
        return self.by_occupancy[0][1]

    def closest(self, tpb):
        '''Find the occupancy of the closest tpb
        '''
        # round to the nearest multiple of warpsize
        warpsize = PHYSICAL_LIMITS[self.cc]['thread_per_warp']
        tpb = ceil(tpb, warpsize)
        # search
        return self.table[tpb][0]

    def best_within(self, mintpb, maxtpb):
        '''Returns the best tpb in the given range inclusively.
        '''
        warpsize = PHYSICAL_LIMITS[self.cc]['thread_per_warp']
        mintpb = int(ceil(mintpb, warpsize))
        maxtpb = int(floor(maxtpb, warpsize))
        return self.prefer(*range(mintpb, maxtpb + 1, warpsize))

    def prefer(self, *tpblist):
        '''Prefer the thread-per-block with the highest warp occupancy 
        and the lowest thread-per-block.
        
        May return None if all threads-per-blocks are invalid
        '''
        bin = []
        for tpb in tpblist:
            occ = self.closest(tpb)
            if occ > 0:
                bin.append((occ, tpb))
        if bin:
            return sorted(bin, cmp=_cmp_occupancy)[-1][1]


#------------------------------------------------------------------------------
# warp occupancy calculator

LIMITS_CC_20 = {
    'thread_per_warp': 32,
    'warp_per_sm'    : 48,
    'thread_per_sm'  : 1536,
    'block_per_sm'   : 8,
    'registers'      : 32768,
    'reg_alloc_unit' : 64,
    'reg_alloc_gran' : 'warp',
    'reg_per_thread' : 63,
    'smem_per_sm'    : 49152,
    'smem_alloc_unit': 128,
    'warp_alloc_gran': 2,
    'max_block_size' : 1024,
}


LIMITS_CC_21 = LIMITS_CC_20

LIMITS_CC_30 = {
    'thread_per_warp': 32,
    'warp_per_sm'    : 64,
    'thread_per_sm'  : 2048,
    'block_per_sm'   : 16,
    'registers'      : 65535,
    'reg_alloc_unit' : 256,
    'reg_alloc_gran' : 'warp',
    'reg_per_thread' : 63,
    'smem_per_sm'    : 49152,
    'smem_alloc_unit': 256,
    'warp_alloc_gran': 4,
    'max_block_size' : 1024,
}

LIMITS_CC_35 = LIMITS_CC_30.copy()
LIMITS_CC_35.update({
    'reg_per_thread' : 255,
})

PHYSICAL_LIMITS = {
    (2, 0): LIMITS_CC_20,
    (2, 1): LIMITS_CC_21,
    (3, 0): LIMITS_CC_30,
    (3, 5): LIMITS_CC_35,
}


def ceil(x, s=1):
    return s * math.ceil(x / s)

def floor(x, s=1):
    return s * math.floor(x / s)

def warp_occupancy(info, cc, smem_config=48 * 2**10):
    '''Returns a dictionary of {threadperblock: occupancy, factor}
    
    Only threadperblock of multiple of warpsize is used.
    Only threadperblock of non-zero occupancy is returned.
    '''
    ret = {}
    limits = PHYSICAL_LIMITS[cc]
    warpsize = limits['thread_per_warp']
    max_thread = limits['max_block_size']

    for tpb in range(warpsize, max_thread, warpsize):
        result = compute_warp_occupancy(tpb=tpb,
                                        reg=info.get('reg', 0),
                                        smem=info.get('shared', 0),
                                        smem_config=smem_config,
                                        limits=limits)
        if result:
            ret[tpb] = result
    return ret

def compute_warp_occupancy(tpb, reg, smem, smem_config, limits):
    assert limits['reg_alloc_gran'] == 'warp', \
                "assume warp register allocation granularity"
    limit_block_per_sm = limits['block_per_sm']
    limit_warp_per_sm = limits['warp_per_sm']
    limit_thread_per_warp = limits['thread_per_warp']
    limit_reg_per_thread = limits['reg_per_thread']
    limit_total_regs = limits['registers']
    limit_total_smem = min(limits['smem_per_sm'], smem_config)
    my_smem_alloc_unit = limits['smem_alloc_unit']
    reg_alloc_unit = limits['reg_alloc_unit']
    warp_alloc_gran = limits['warp_alloc_gran']

    my_warp_per_block = ceil(tpb / limit_thread_per_warp)
    my_reg_count = reg
    my_reg_per_block = my_warp_per_block
    my_smem = smem
    my_smem_per_block = ceil(my_smem, my_smem_alloc_unit)

    # allocated resource
    limit_blocks_due_to_warps = min(limit_block_per_sm,
                                    floor(limit_warp_per_sm / my_warp_per_block))


    c39 = floor(limit_total_regs / ceil(my_reg_count * limit_thread_per_warp,
                                        reg_alloc_unit),
                warp_alloc_gran)

    limit_blocks_due_to_regs = (0
                                if my_reg_count > limit_reg_per_thread
                                else (floor(c39 / my_reg_per_block)
                                      if my_reg_count > 0
                                      else limit_block_per_sm))

    limit_blocks_due_to_smem = (floor(limit_total_smem /
                                      my_smem_per_block)
                                if my_smem_per_block > 0
                                else limit_block_per_sm)

    # occupancy
    active_block_per_sm = min(limit_blocks_due_to_smem,
                              limit_blocks_due_to_warps,
                              limit_blocks_due_to_regs)
                    
    if active_block_per_sm == limit_blocks_due_to_warps:
        factor = 'warps'
    elif active_block_per_sm == limit_blocks_due_to_regs:
        factor = 'regs'
    else:
        factor = 'smem'


    active_warps_per_sm = active_block_per_sm * my_warp_per_block
    #active_threads_per_sm = active_warps_per_sm * limit_thread_per_warp

    occupancy = active_warps_per_sm / limit_warp_per_sm
    return occupancy, factor

#------------------------------------------------------------------------------
# compile info parsing

def _sw(s):
    return s.replace(' ', r'\s+')

def _regex(s):
    return re.compile(_sw(s), re.I)

RE_LEAD     = _regex(r'^(?:ptxas )?info : Function properties for ')
RE_REG      = _regex(r'used (?P<num>\d+) registers')
RE_STACK    = _regex(r'(?P<num>\d+) (?:bytes )?stack')
RE_SHARED   = _regex(r'(?P<num>\d+) bytes smem')
RE_LOCAL    = _regex(r'(?P<num>\d+) bytes lmem')

def parse_compile_info(text):
    return dict(gen_parse_compile_info(text))

def gen_parse_compile_info(text):
    '''Generator that returns function (name, resource dict)
        
    May yield the same function more than once.  
    In that case, the latter should replace the prior.
    
    Usage:
    
    >>> dict(parse_compile_info(compile_info))
    
    '''
    lines = text.splitlines()
    readline = iter(lines).next

    try:
        ln = readline()
        while True:
            m = RE_LEAD.match(ln)
            if not m:
                # not a lead line; continue
                ln = readline()
                continue
            # start parsing information
            remaining = ln[len(m.group(0)):]
            # function name
            fname = parse_function_name(remaining)
            # resource info
            ln = readline()
            resources = {}
            try:
                while True:
                    more = False
                    for section in ln.split(','):
                        res = parse_resources(section)
                        if res:
                            k, v = res
                            resources[k] = v
                            more = True
                    ln = readline()
                    if not more:
                        break
            finally:
                yield fname, resources
    except StopIteration:
        pass

def parse_function_name(text):
    name = text.strip().rstrip(':')
    # has quote?
    if name.startswith("'"):
        assert name.endswith("'")
        name = name[1:-1]
    return name

def parse_resources(text):
    '''
    Returns (key, value) tuple on successful parse;
            otherwise, None
    '''
    relst = [('reg',    RE_REG),
             ('stack',  RE_STACK),
             ('shared', RE_SHARED),
             ('local',  RE_LOCAL)]
    for resname, regex in relst:
        m = regex.search(text)
        if m:
            key = resname
            val = int(m.group('num'))
            return key, val
