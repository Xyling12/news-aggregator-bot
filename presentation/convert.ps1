$ppt = New-Object -ComObject PowerPoint.Application
$ppt.Visible = [Microsoft.Office.Core.MsoTriState]::msoTrue
$pres = $ppt.Presentations.Open("D:\Antigravity\news-aggregator-bot-main\presentation\presentation.pptx")
$pres.SaveAs("D:\Antigravity\news-aggregator-bot-main\presentation\presentation.pdf", 32)
$pres.Close()
$ppt.Quit()
