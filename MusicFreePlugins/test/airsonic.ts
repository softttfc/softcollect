import plugin from '../plugins/airsonic/index';

// 模拟环境变量
global.env = {
  getUserVariables: () => ({
    url: 'http://your-airsonic-server.com',
    username: 'your-username',
    password: 'your-password'
  })
};

async function testPlugin() {
  console.log('Testing Airsonic Advanced Plugin...');
  
  try {
    // 测试初始化和服务器连接
    console.log('\n=== 测试服务器连接 ===');
    const initResult = await plugin.init();
    console.log('Init result:', JSON.stringify(initResult, null, 2));
    
    if (!initResult.connected) {
      console.log('服务器连接失败，跳过其他测试');
      return;
    }
    
    console.log(`认证方式: ${initResult.authMethod}`);
    console.log(`服务器类型: ${initResult.type}`);
    console.log(`API版本: ${initResult.version}`);
    
    // 测试搜索音乐
    console.log('\n=== 测试搜索音乐 ===');
    const musicResults = await plugin.search('test', 1, 'music');
    console.log('Music search results:', JSON.stringify(musicResults, null, 2));
    
    // 测试搜索专辑
    console.log('\n=== 测试搜索专辑 ===');
    const albumResults = await plugin.search('test', 1, 'album');
    console.log('Album search results:', JSON.stringify(albumResults, null, 2));
    
    // 测试搜索艺术家
    console.log('\n=== 测试搜索艺术家 ===');
    const artistResults = await plugin.search('test', 1, 'artist');
    console.log('Artist search results:', JSON.stringify(artistResults, null, 2));
    
    // 测试获取榜单
    console.log('\n=== 测试获取榜单 ===');
    const topLists = await plugin.getTopLists();
    console.log('Top lists:', JSON.stringify(topLists, null, 2));
    
  } catch (error) {
    console.error('测试失败:', error);
  }
}

// 只有在直接运行此文件时才执行测试
if (require.main === module) {
  testPlugin();
}

export default testPlugin;