
# 创建示例npz文件
save_path = 'data/wave_20260530_181216.npz'


import numpy as np
from collections import Counter

def diagnose_npz_dataset(npz_path):
    """诊断npz文件中的dataset形状问题"""
    data = np.load(npz_path)
    dataset = data['dataset']
    
    print("="*60)
    print(f"诊断文件: {npz_path}")
    print("="*60)
    print(f"外层形状: {dataset.shape}")
    print(f"数据类型: {dataset.dtype}")
    
    if dataset.dtype == object:
        print("\n⚠️  检测到对象数组（一维数组，每个元素是独立数组）")
        
        # 检查每个子数组的形状
        shapes = []
        for i, item in enumerate(dataset):
            if hasattr(item, 'shape'):
                shapes.append(item.shape)
            else:
                shapes.append(None)
                print(f"  样本{i}不是数组: {type(item)}")
        
        # 统计形状分布
        shape_counts = Counter(map(str, shapes))
        print(f"\n形状分布:")
        for shape, count in shape_counts.items():
            print(f"  {shape}: {count}个样本")
        
        # 给出建议
        if len(shape_counts) == 1:
            print("\n✅ 所有子数组形状相同！")
            print(f"建议使用: np.stack(dataset) 转换为形状 {dataset[0].shape}")
            print(f"转换后形状: ({len(dataset)}, {dataset[0].shape})")
        else:
            print("\n❌ 子数组形状不一致")
            print("建议: 使用填充(padding)或截断(truncation)统一形状")
    
    return data

# 使用诊断
data = diagnose_npz_dataset(save_path)