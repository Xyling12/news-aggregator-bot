const pptxgen = require('pptxgenjs');

async function createPresentation() {
    const pptx = new pptxgen();
    pptx.layout = 'LAYOUT_16x9';
    pptx.author = 'Ижевск Сегодня';
    pptx.company = 'Ижевск Сегодня';
    pptx.title = 'Бизнес-план';

    const dark = '0D0D0D';
    const dark2 = '1A1A1A';
    const red = 'C8102E';
    const gold = 'F5A623';
    const white = 'FFFFFF';
    const gray = '8E8E93';

    pptx.defineSlideMaster({
      title: 'MASTER_SLIDE',
      background: { color: dark },
      objects: [
        { rect: { x: 0, y: 0, w: '100%', h: 0.1, fill: { color: red } } },
        { text: { text: 'ИЖЕВСК СЕГОДНЯ · БИЗНЕС-ПЛАН · МАРТ 2026', options: { x: 0.5, y: 5.3, w: 9, h: 0.3, color: '404040', fontSize: 10, align: 'left' } } }
      ]
    });

    // Slide 1: Cover
    let slide1 = pptx.addSlide({ masterName: 'MASTER_SLIDE' });
    slide1.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: '100%', h: '100%', fill: { color: dark } });
    
    slide1.addText('КОНФИДЕНЦИАЛЬНО · ДЛЯ ИНВЕСТОРОВ', { x: 0.5, y: 1.5, w: 9, h: 0.5, color: red, fontSize: 11, bold: true, align: 'center' });
    slide1.addText('ИС', { x: 0.5, y: 2.0, w: 9, h: 1.0, color: red, fontSize: 80, bold: true, align: 'center' });
    slide1.addText('Ижевск Сегодня', { x: 0.5, y: 3.2, w: 9, h: 0.6, color: white, fontSize: 36, bold: true, align: 'center' });
    slide1.addText('Региональный AI-медиапроект', { x: 0.5, y: 3.8, w: 9, h: 0.5, color: gray, fontSize: 18, align: 'center' });

    slide1.addText([
        { text: 'Стадия: ', options: { color: gray } }, { text: 'MVP → Рост   |   ', options: { color: white, bold: true } },
        { text: 'Инвестиции: ', options: { color: gray } }, { text: '500–750k ₽   |   ', options: { color: white, bold: true } },
        { text: 'ROI / 2 года: ', options: { color: gray } }, { text: '200–400%   |   ', options: { color: white, bold: true } },
        { text: 'Маржа: ', options: { color: gray } }, { text: '~75%', options: { color: white, bold: true } }
    ], { x: 0.5, y: 4.8, w: 9, h: 0.5, fontSize: 13, align: 'center' });

    // Slide 2: Problem & Solution
    let slide2 = pptx.addSlide({ masterName: 'MASTER_SLIDE' });
    slide2.addText('01 / Проблема и решение', { x: 0.5, y: 0.5, w: 4, h: 0.3, color: red, fontSize: 12, bold: true });
    slide2.addText('Региональные новости отстали на 10 лет', { x: 0.5, y: 0.8, w: 9, h: 0.6, color: white, fontSize: 28, bold: true });
    
    slide2.addText('Традиционные сайты Ижевска публикуют 3–7 материалов в день с задержкой 2–4 часа. Аудитория уходит в Telegram — но качественного регионального контента там почти нет.', 
        { x: 0.5, y: 1.5, w: 9, h: 0.6, color: 'CCCCCC', fontSize: 14 }
    );

    // Cards
    slide2.addShape(pptx.ShapeType.rect, { x: 0.5, y: 2.3, w: 2.1, h: 1, fill: { color: dark2 }, line: { color: '333333' } });
    slide2.addText('Аудитория Ижевска\n650 000+', { x: 0.5, y: 2.3, w: 2.1, h: 1, color: white, fontSize: 16, align: 'center', bold: true });
    
    slide2.addShape(pptx.ShapeType.rect, { x: 2.8, y: 2.3, w: 2.1, h: 1, fill: { color: dark2 }, line: { color: '333333' } });
    slide2.addText('Конкуренты\n3–7 постов/день', { x: 2.8, y: 2.3, w: 2.1, h: 1, color: red, fontSize: 16, align: 'center', bold: true });

    slide2.addShape(pptx.ShapeType.rect, { x: 5.1, y: 2.3, w: 2.1, h: 1, fill: { color: dark2 }, line: { color: '333333' } });
    slide2.addText('Наши посты\n24–30/день', { x: 5.1, y: 2.3, w: 2.1, h: 1, color: gold, fontSize: 16, align: 'center', bold: true });

    slide2.addShape(pptx.ShapeType.rect, { x: 7.4, y: 2.3, w: 2.1, h: 1, fill: { color: dark2 }, line: { color: '333333' } });
    slide2.addText('Автоматизация\n~95%', { x: 7.4, y: 2.3, w: 2.1, h: 1, color: white, fontSize: 16, align: 'center', bold: true });

    slide2.addText('Наше решение', { x: 0.5, y: 3.5, w: 9, h: 0.4, color: white, fontSize: 18, bold: true });
    slide2.addText([
        { text: '• Мониторинг 7+ источников в реальном времени\n' },
        { text: '• AI-рерайт чере Gemini (совершенно без плагиата)\n' },
        { text: '• Кросспостинг: Telegram + ВКонтакте + Дзен\n' },
        { text: '• Умная AI дедупликация (из 5 источников 1 пост)' }
    ], { x: 0.5, y: 4.0, w: 9, h: 1.2, color: 'CCCCCC', fontSize: 14, bullet: true });

    // Slide 3: Market & Competitors
    let slide3 = pptx.addSlide({ masterName: 'MASTER_SLIDE' });
    slide3.addText('02 / Рынок и конкуренты', { x: 0.5, y: 0.5, w: 4, h: 0.3, color: red, fontSize: 12, bold: true });
    slide3.addText('Ни одного сильного TG-канала на 650k человек', { x: 0.5, y: 0.8, w: 9, h: 0.6, color: white, fontSize: 26, bold: true });

    const tableRows = [
        [{ text: 'Канал', options: { color: red, bold: true } }, { text: 'TG-подписчики', options: { color: red, bold: true } }, { text: 'Постов/день', options: { color: red, bold: true } }, { text: 'Автоматизация', options: { color: red, bold: true } }],
        ['izhlife.ru', '8 000+', '5–10', 'Нет'],
        ['udm-info.ru', '5 000+', '3–7', 'Нет'],
        ['Ижевск Онлайн', '12 000+', '8–12', 'Частично'],
        [{ text: 'Ижевск Сегодня', options: { bold: true } }, { text: 'Растёт', options: { bold: true } }, { text: '24–30', options: { bold: true } }, { text: '95% (AI)', options: { bold: true, color: '34C759' } }]
    ];

    slide3.addTable(tableRows, {
        x: 0.5, y: 1.8, w: 9, colW: [2.5, 2.5, 2, 2],
        fill: { color: dark2 }, border: { pt: 1, color: '333333' },
        rowH: 0.5, fontSize: 14, color: white, align: 'center', valign: 'middle'
    });

    slide3.addShape(pptx.ShapeType.rect, { x: 0.5, y: 4.2, w: 9, h: 0.8, fill: { color: '2A0D11' }, border: { pt: 1, color: '5A1A24' }});
    slide3.addText('Ключевое преимущество работает 24/7 без редакции. Себестоимость одной публикации — ~2–5 ₽ (API-вызовы). У традиционных СМИ с журналистом — 500–2000 ₽.', 
        { x: 0.6, y: 4.3, w: 8.8, h: 0.6, color: white, fontSize: 13, align: 'left' }
    );

    // Slide 4: Financials
    let slide4 = pptx.addSlide({ masterName: 'MASTER_SLIDE' });
    slide4.addText('03 / Монетизация', { x: 0.5, y: 0.5, w: 4, h: 0.3, color: red, fontSize: 12, bold: true });
    slide4.addText('Три источника дохода, маржа ~75%', { x: 0.5, y: 0.8, w: 9, h: 0.6, color: white, fontSize: 28, bold: true });

    const finRows = [
        [{ text: 'Период', options: { color: red, bold: true } }, { text: 'Подписчики', options: { color: red, bold: true } }, { text: 'Расходы/мес', options: { color: red, bold: true } }, { text: 'Выручка/мес', options: { color: red, bold: true } }, { text: 'Прибыль/мес', options: { color: red, bold: true } }],
        ['6 мес.', '3 000–5 000', '10 000 ₽', '30–60 тыс. ₽', '20–50 тыс. ₽'],
        ['12 мес.', '10 000–15 000', '15 000 ₽', '120–250 тыс. ₽', '105–235 тыс. ₽'],
        ['24 мес.', '30 000–50 000', '30 000 ₽', '400–800 тыс. ₽', '370–770 тыс. ₽']
    ];

    slide4.addTable(finRows, {
        x: 0.5, y: 1.8, w: 9, colW: [1.5, 2, 1.5, 2, 2],
        fill: { color: dark2 }, border: { pt: 1, color: '333333' },
        rowH: 0.5, fontSize: 13, color: white, align: 'center', valign: 'middle'
    });

    slide4.addText('Текущие операционные расходы (MVP)', { x: 0.5, y: 3.5, w: 9, h: 0.4, color: white, fontSize: 16, bold: true });
    
    slide4.addShape(pptx.ShapeType.rect, { x: 0.5, y: 4.1, w: 2.1, h: 0.8, fill: { color: dark2 }, line: { color: '333333' } });
    slide4.addText('VPS / Dokploy\n1 500 ₽', { x: 0.5, y: 4.1, w: 2.1, h: 0.8, color: white, fontSize: 14, align: 'center', bold: true });
    
    slide4.addShape(pptx.ShapeType.rect, { x: 2.8, y: 4.1, w: 2.1, h: 0.8, fill: { color: dark2 }, line: { color: '333333' } });
    slide4.addText('Gemini API\n3–8 тыс. ₽', { x: 2.8, y: 4.1, w: 2.1, h: 0.8, color: white, fontSize: 14, align: 'center', bold: true });

    slide4.addShape(pptx.ShapeType.rect, { x: 5.1, y: 4.1, w: 2.1, h: 0.8, fill: { color: dark2 }, line: { color: '333333' } });
    slide4.addText('Домен / прочее\n200 ₽', { x: 5.1, y: 4.1, w: 2.1, h: 0.8, color: white, fontSize: 14, align: 'center', bold: true });

    slide4.addShape(pptx.ShapeType.rect, { x: 7.4, y: 4.1, w: 2.1, h: 0.8, fill: { color: dark2 }, line: { color: red } });
    slide4.addText('ИТОГО / мес.\n~10 000 ₽', { x: 7.4, y: 4.1, w: 2.1, h: 0.8, color: gold, fontSize: 14, align: 'center', bold: true });

    // Slide 5: Investment Request
    let slide5 = pptx.addSlide({ masterName: 'MASTER_SLIDE' });
    slide5.addText('04 / Инвестиционный запрос', { x: 0.5, y: 0.5, w: 4, h: 0.3, color: red, fontSize: 12, bold: true });
    slide5.addText('Раунд A: масштабирование и рост', { x: 0.5, y: 0.8, w: 9, h: 0.6, color: white, fontSize: 28, bold: true });

    slide5.addShape(pptx.ShapeType.rect, { x: 0.5, y: 1.5, w: 9, h: 1.5, fill: { color: '1A0306' }, border: { pt: 1, color: red } });
    slide5.addText('Объём привлечения', { x: 0.5, y: 1.7, w: 9, h: 0.3, color: gray, fontSize: 12, align: 'center' });
    slide5.addText('500–750 тыс. ₽', { x: 0.5, y: 2.0, w: 9, h: 0.6, color: white, fontSize: 36, bold: true, align: 'center' });
    slide5.addText('Доля для инвестора: 20–35% · Горизонт окупаемости: 12–18 мес.', { x: 0.5, y: 2.6, w: 9, h: 0.3, color: gold, fontSize: 13, align: 'center' });

    slide5.addText('Использование средств:', { x: 0.5, y: 3.3, w: 4, h: 0.3, color: white, fontSize: 14, bold: true });
    slide5.addText([
        { text: '• Маркетинг (Пиар) — 150 000 ₽\n' },
        { text: '• Редактор спецпроектов на 6 мес — 100 000 ₽\n' },
        { text: '• Масштабирование на другие города — 200 000 ₽' }
    ], { x: 0.5, y: 3.8, w: 4.5, h: 1, color: 'CCCCCC', fontSize: 12, bullet: true });

    slide5.addText('Контакты связи:', { x: 5.5, y: 3.3, w: 4, h: 0.3, color: white, fontSize: 14, bold: true });
    slide5.addText([
        { text: 'TG: ', options: { color: gray } }, { text: '@IzhevskTodayNews\n', options: { color: white } },
        { text: 'VK: ', options: { color: gray } }, { text: 'vk.com/club236380336\n', options: { color: white } },
        { text: 'Бот: ', options: { color: gray } }, { text: '@NewsRussain11_bot\n', options: { color: white } }
    ], { x: 5.5, y: 3.8, w: 4, h: 1, color: 'CCCCCC', fontSize: 13 });

    await pptx.writeFile({ fileName: 'presentation.pptx' });
}

createPresentation().catch(console.error);
