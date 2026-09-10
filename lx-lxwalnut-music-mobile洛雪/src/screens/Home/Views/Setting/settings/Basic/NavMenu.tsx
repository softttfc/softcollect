import { memo, useMemo, useState, useCallback } from 'react';
import { StyleSheet, View, Text, Pressable } from 'react-native';
import SubTitle from '../../components/SubTitle';
import CheckBox from '@/components/common/CheckBox';
import { useSettingValue } from '@/store/setting/hook';
import { useI18n } from '@/lang';
import { updateSetting } from '@/core/common';
import { NAV_MENUS, NAV_ID_Type } from '@/config/constant';
import { useTheme } from '@/store/theme/hook';

const CANNOT_CLOSE_ITEMS: NAV_ID_Type[] = ['nav_setting'];

interface MenuItemData {
  id: NAV_ID_Type;
  name: string;
}

const MenuItem = memo(({
  item,
  index,
  isChecked,
  onToggle,
  onMoveUp,
  onMoveDown,
  isLast,
}: {
  item: MenuItemData;
  index: number;
  isChecked: boolean;
  onToggle: (id: NAV_ID_Type, check: boolean) => void;
  onMoveUp: (index: number) => void;
  onMoveDown: (index: number) => void;
  isLast: boolean;
}) => {
  const theme = useTheme();
  const cannotClose = CANNOT_CLOSE_ITEMS.includes(item.id);

  return (
    <View style={styles.menuItem}>
      <View style={styles.menuInfo}>
        <Text style={[styles.menuName, { color: theme['c-font'] }]}>{item.name}</Text>
        <View style={styles.controls}>
          <Pressable
            style={styles.moveBtn}
            onPress={() => onMoveUp(index)}
            disabled={index === 0}
          >
            <Text style={[styles.moveBtnText, { color: index === 0 ? theme['c-font-label'] : theme['c-font'] }]}>
              ↑
            </Text>
          </Pressable>
          <Pressable
            style={styles.moveBtn}
            onPress={() => onMoveDown(index)}
            disabled={isLast}
          >
            <Text style={[styles.moveBtnText, { color: isLast ? theme['c-font-label'] : theme['c-font'] }]}>
              ↓
            </Text>
          </Pressable>
          <CheckBox
            check={isChecked}
            label=""
            disabled={cannotClose}
            onChange={(check) => onToggle(item.id, check)}
          />
        </View>
      </View>
    </View>
  );
});

export default memo(() => {
  const t = useI18n();
  const theme = useTheme();
  const navStatus = useSettingValue('common.navStatus');
  const navOrder = useSettingValue('common.navOrder');
  const picOpacity = useSettingValue('theme.picOpacity');

  const [localOrder, setLocalOrder] = useState<NAV_ID_Type[]>(() => {
    return navOrder || NAV_MENUS.map(m => m.id);
  });

  const menuList = useMemo(() => {
    return localOrder
      .filter(id => id !== 'nav_play_history')
      .map((id, idx) => {
        const menuItem = NAV_MENUS.find(m => m.id === id);
        return {
          id,
          name: t(id),
        };
      });
  }, [localOrder, t]);

  const handleToggle = useCallback((id: NAV_ID_Type, check: boolean) => {
    updateSetting({ 'common.navStatus': { ...navStatus, [id]: check } });
  }, [navStatus]);

  const handleMoveUp = useCallback((index: number) => {
    if (index <= 0) return;
    const newOrder = [...localOrder];
    
    const filteredList = newOrder.filter(id => id !== 'nav_play_history');
    const itemId = filteredList[index];
    
    const realIndex = newOrder.indexOf(itemId);
    if (realIndex <= 0) return;
    
    const item = newOrder.splice(realIndex, 1)[0];
    newOrder.splice(realIndex - 1, 0, item);
    
    setLocalOrder(newOrder);
    updateSetting({ 'common.navOrder': newOrder });
  }, [localOrder]);

  const handleMoveDown = useCallback((index: number) => {
    const newOrder = [...localOrder];
    
    const filteredList = newOrder.filter(id => id !== 'nav_play_history');
    if (index >= filteredList.length - 1) return;
    
    const itemId = filteredList[index];
    const realIndex = newOrder.indexOf(itemId);
    
    const nextItemId = filteredList[index + 1];
    const nextRealIndex = newOrder.indexOf(nextItemId);
    
    const item = newOrder.splice(realIndex, 1)[0];
    newOrder.splice(nextRealIndex, 0, item);
    
    setLocalOrder(newOrder);
    updateSetting({ 'common.navOrder': newOrder });
  }, [localOrder]);

  return (
    <SubTitle title={t('setting_basic_nav_menu')}>
      <View style={styles.container}>
        <View style={styles.tipContainer}>
          <Text style={[styles.tipText, { color: theme['c-font-label'] }]}>
            点击「↑↓」调整顺序，☐控制显示/隐藏
          </Text>
        </View>
        <View style={{ 
          overflow: 'hidden', borderRadius: 8, 
          backgroundColor: 'transparent',
        }}>
          <View style={styles.menuList}>
            {menuList.map((item, idx) => (
              <MenuItem
                key={item.id}
                item={item}
                index={idx}
                isChecked={navStatus[item.id] ?? true}
                onToggle={handleToggle}
                onMoveUp={handleMoveUp}
                onMoveDown={handleMoveDown}
                isLast={idx === menuList.length - 1}
              />
            ))}
          </View>
        </View>
      </View>
    </SubTitle>
  );
});

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  tipContainer: {
    paddingHorizontal: 10,
    paddingVertical: 8,
    marginBottom: 8,
  },
  tipText: {
    fontSize: 13,
  },
  menuList: {
    overflow: 'hidden',
  },
  menuItem: {
    paddingVertical: 12,
    paddingHorizontal: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: 'rgba(128, 128, 128, 0.2)',
  },
  menuInfo: {
    flex: 1,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  menuName: {
    fontSize: 16,
    flex: 1,
  },
  lockedText: {
    fontSize: 12,
    marginRight: 10,
  },
  controls: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  moveBtn: {
    padding: 4,
    minWidth: 28,
    height: 28,
    justifyContent: 'center',
    alignItems: 'center',
  },
  moveBtnText: {
    fontSize: 18,
    fontWeight: 'bold',
  },
});
