async page => {
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('http://127.0.0.1:17894/#automations-section');
  await page.locator('#automation-new').click();
  await page.locator('#auto-name').fill('验收：每日工作提醒');
  await page.locator('#auto-content').fill('检查今天的 Claude 任务和待审查 PR。');
  await page.locator('#auto-schedule').selectOption('daily');
  if(await page.locator('#auto-interval-field').isVisible() || await page.locator('#auto-threshold-field').isVisible())throw Error('Irrelevant fields are visible');
  await page.screenshot({path:'work/automation-editor.png'});
  await page.locator('#auto-save').click();
  await page.locator('#automation-dialog').waitFor({state:'hidden'});
  await page.locator('[data-preview]').first().click();
  await page.locator('#automation-runs details').first().waitFor();
  await page.locator('#automation-runs details').first().evaluate(el=>el.open=true);
  await page.getByText('试运行 · 未发送飞书', {exact:false}).first().waitFor();
  await page.locator('#automation-list [data-auto-toggle]').first().click();
  await page.locator('#automation-list').getByRole('button',{name:'暂停',exact:true}).waitFor();
  await page.locator('#automation-list').getByRole('button',{name:'暂停',exact:true}).click();
  await page.locator('#automation-list').getByRole('button',{name:'启用',exact:true}).first().waitFor();
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:'work/automation-overview.png',fullPage:true});
  const views=['overview','accounts','analytics','windows','resources','activity','routes','lark','automations'];
  for(const view of views){
    await page.locator(`[data-nav="${view}"]`).click();
    if(!await page.locator(`[data-view="${view}"]`).first().isVisible())throw Error('View not visible: '+view);
  }
  await page.locator('#theme-toggle').click();
  await page.screenshot({path:'work/automation-dark.png',fullPage:true});
  await page.locator('#theme-toggle').click();
  await page.setViewportSize({width:390,height:844});
  const overflow=[];
  for(const view of views){
    await page.locator(`[data-nav="${view}"]`).click();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))overflow.push(view);
  }
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:'work/automation-mobile.png',fullPage:true});
  if(errors.length || overflow.length)throw Error(JSON.stringify({errors,overflow}));
  return {views:views.length,errors,overflow,flow:'created, previewed, enabled, paused; no messages sent'};
}
