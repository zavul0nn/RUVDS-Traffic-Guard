"""Resource charts against local preview.py: synthetic samples, no production control."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as pw:
    browser=pw.chromium.launch()
    context=browser.new_context(viewport={'width':1440,'height':1060})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto('http://127.0.0.1:8789/preview-hidden-path-0123456789')
    page.locator('#password').fill('preview-password-123');page.locator('#login-form button').click()
    expect(page.locator('#resource-cpu .resource-current')).not_to_have_text('—')
    page.wait_for_function('resourceRows.length>2800')
    page.locator('.resource-section').scroll_into_view_if_needed()
    page.evaluate('window.resourceSavedRows=resourceRows.slice()')
    for index,key in enumerate(('cpu','memory')):
        peak=page.evaluate('(i)=>{const c=resourceCharts[i];let index=0;c.points.forEach((row,j)=>{if(c.value(row)>c.value(c.points[index]))index=j;});return {index,formatted:c.format(c.points[index]),time:dateTime(c.points[index][0])+" МСК",x:c.x(c.points[index][0]),y:c.y(c.value(c.points[index]))};}',index)
        box=page.locator('#resource-'+key+' svg').bounding_box()
        page.mouse.move(box['x']+peak['x']/800*box['width'],box['y']+peak['y']/240*box['height'])
        expect(page.locator('#resource-tip-'+key)).to_be_visible()
        expect(page.locator('#resource-tip-'+key+' b')).to_have_text(peak['formatted'])
        expect(page.locator('#resource-tip-'+key+' strong')).to_have_text(peak['time'])
        page.locator('#resource-'+key+' svg').focus();page.keyboard.press('Home')
        expect(page.locator('#resource-tip-'+key)).to_be_visible()
        page.keyboard.press('End');page.keyboard.press('ArrowLeft');page.keyboard.press('Escape')
        expect(page.locator('#resource-tip-'+key)).not_to_be_visible()
        page.locator('#resource-'+key+' [data-resource-hours="1"]').click()
        assert page.evaluate('(i)=>resourceCharts[i].points.length',index)<125
        page.locator('#resource-'+key+' [data-resource-hours="24"]').click()
    response=page.evaluate('async()=>{const since=resourceRows[resourceRows.length-1][0];return (await (await fetch(base+"api/resources?since="+since)).json()).samples;}')
    assert len(response)<=1,'Incremental API returned entire history'
    page.evaluate('resourceRows=[];resourceCharts.forEach(c=>c.render())')
    expect(page.locator('#resource-cpu .resource-empty')).to_be_visible()
    page.evaluate('resourceRows=window.resourceSavedRows;resourceCharts.forEach(c=>c.render())')
    page.locator('.resource-section').screenshot(path=str(Path('artifacts/resources-desktop.png')))
    mobile=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
    mobile.add_cookies(context.cookies());touch=mobile.new_page()
    touch.on('pageerror',lambda error:errors.append(str(error)))
    touch.goto('http://127.0.0.1:8789/preview-hidden-path-0123456789')
    expect(touch.locator('#resource-cpu .resource-current')).not_to_have_text('—')
    touch.locator('#resource-cpu svg').scroll_into_view_if_needed()
    box=touch.locator('#resource-cpu svg').bounding_box()
    touch.touchscreen.tap(box['x']+box['width']*.55,box['y']+box['height']*.5)
    expect(touch.locator('#resource-tip-cpu')).to_be_visible()
    assert touch.evaluate('document.documentElement.scrollWidth<=innerWidth')
    touch.locator('.resource-section').screenshot(path=str(Path('artifacts/resources-mobile.png')))
    assert not errors,errors
    browser.close()
print('Resource UI QA passed: measured CPU/RAM tooltips, keyboard, time ranges, incremental API, empty history, mobile touch; no JS errors.')
