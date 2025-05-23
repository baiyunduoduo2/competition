import logging
from typing import List
# manage simple kv cache

# provided by 朱国栋 00574805
# 实现了一个基于块分配策略的内存池。该类将一大块内存划分成多个固定大小的块，每次内存申请时根据请求的大小分配相应数量的块。
class BlockMemPool:
    """
    block-wise memory pool
    reuse memory at the granularity of block.

    memory view is as follows:

    the whole memory area(big enough) would be split to 'num_blocks' blocks,
    each contains

    +------+------+------+------+
    | b0s0 | b0s1 | b0s2 | b0s3 |
    +------+------+------+------+
    | b1s0 | b1s1 | b1s2 | b1s3 |
    +------+------+------+------+
    | b2s0 | b2s1 | b2s2 | b2s3 |
    +------+------+------+------+

    """
    def __init__(self, num_blocks: int, block_size: int):
        self.num_blocks = num_blocks # 当前可用的内存块的数量
        self.block_size = block_size
        self.free_blocks = [i for i in range(num_blocks)]
        self.used_blocks = []
        logging.debug("reducing global adundant block...")
        self.allocate_block(1)

    def allocate_block(self, num_new_block: int):
        # 分配指定数量的块。
        if len(self.free_blocks) < num_new_block:
            raise RuntimeError('block pool is out of memory')
        # 从空闲块中移除指定数量的块并将它们添加到已使用块中。
        new_blocks = self.free_blocks[0:num_new_block]
        self.used_blocks += new_blocks
        self.free_blocks = self.free_blocks[num_new_block:]
        self.num_blocks -= len(new_blocks)
        return new_blocks

    def free_block(self, block_indices: List[int]):
        # 释放指定的块，将它们从已使用块列表中移除，并添加回空闲块列表。
        for idx in block_indices:
            if idx not in self.used_blocks:
                raise RuntimeError(f"bad block idx, {idx} is not in the used block list.")
            self.free_blocks.append(idx)
            self.num_blocks += 1
            self.used_blocks.remove(idx)

# 主要负责管理内存块的分配、释放以及缓存的更新。
class CacheEngine:
    def __init__(self, block_size: int, pool: BlockMemPool=None):
        # allocate a big chunk memory
        self.block_size = block_size
        self.pool = pool
        self.num_token = 0 #存了多少token
        self.block_table = []

    def prepare_cache(self, num_new_token):
        # 为新的 token 申请缓存。如果现有的块不够存储新 token，需要向内存池申请更多的块。
        num_blocks = len(self.block_table)
        remained_token = num_blocks * self.block_size - self.num_token

        if remained_token < num_new_token:
            # free block slot is not enough, allocate more blocks.
            num_new_block = (num_new_token - remained_token + self.block_size - 1) // self.block_size
            new_block = self.pool.allocate_block(num_new_block)
            self.block_table += new_block

        # update token num
        self.num_token += num_new_token

    def release_cache(self):
        # 释放全部当前缓存使用的块，并将它们归还给内存池。
        self.pool.free_block(self.block_table)
        self.block_table = []
        self.num_token = 0

# 增强上述接口

serving_block_mem_pool = None  # 全局变量
# 加入了全局管理机制并增加了内存预算管理
class ServingBlockMemPool(BlockMemPool):
    def __init__(self, num_blocks: int, block_size: int):
        super().__init__(num_blocks, block_size)
        self.num_budget_blocks = self.num_blocks

    def reset_budget(self):
        self.num_budget_blocks = self.num_blocks

    def log_status(self):
        logging.debug("mem pool status: budget blocks: %s, actual free blocks: %s", self.num_budget_blocks, self.num_blocks)

    @staticmethod
    def instance(): 
        global serving_block_mem_pool 
        if not serving_block_mem_pool:  # 创建单例，全局变量被改写
            raise RuntimeError("global block mem pool has not been initialized!")
        return serving_block_mem_pool
    
    @staticmethod
    def init(num_blocks, block_size):
        global serving_block_mem_pool
        if serving_block_mem_pool is not None:
            raise RuntimeError("global block mem pool has been initialized already!")
        serving_block_mem_pool = ServingBlockMemPool(num_blocks, block_size)
        logging.info("successfully initialized global memory pool")
# 主要用于服务阶段的缓存管理，尤其是推理任务中的缓存资源分配和管理。
class ServingCacheEngine(CacheEngine):
    def __init__(self, block_size: int, pool: BlockMemPool=None):
        super().__init__(block_size, pool)
        self.num_budget_used = 0
    
    def try_use_budget(self, num_new_token):
        # 尝试使用预算块。根据新的 token 数量计算需要的块数，判断是否有足够的预算块。
        """用于schedule阶段，调用模型推理之前"""
        num_blocks = len(self.block_table)
        remained_token = num_blocks * self.block_size - self.num_token
        # 当前尾部block的剩余slot仍然够用
        if num_new_token <= remained_token:
            return True

        # free block slot is not enough, allocate more blocks.
        num_new_block = (num_new_token - remained_token + self.block_size - 1) // self.block_size
        # logging.debug("try use budget requires num of blocks: %s, remaining: %s", num_new_block, self.pool.num_budget_blocks)
        if num_new_block <= self.pool.num_budget_blocks:
            self.num_budget_used += num_new_block
            self.pool.num_budget_blocks -= num_new_block
            return True
        else:
            return False

    def assign_null_block(self):
        """用于非running状态的请求，将其占用block数量降到最低"""
        self.num_token = 1
        self.block_table = [0]

    def num_blocks(self):
        return len(self.block_table)

    def compute_required_num_block(self, num_tokens: int):
        # 根据 token 数量计算需要的块数。
        if num_tokens % self.block_size:
            return num_tokens // self.block_size + 1
        else:
            return num_tokens // self.block_size

    def release_budget(self):
        self.pool.num_budget_blocks += self.num_budget_used
        self.num_budget_used = 0
        
    def release_cache(self):
        num_blocks_to_free = len(self.block_table)
        self.pool.num_budget_blocks += num_blocks_to_free
        super().release_cache()



