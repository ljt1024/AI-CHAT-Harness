import { useScreenshot } from '@/shared/hooks/useScreenshot';
import { useLanguage } from '@/app/providers/LanguageContext';
import { createPortal } from 'react-dom';
import PictuerIcon from '@/shared/assets/pictuer.png'

import './index.css'

interface ShareProps {
    targetElement: HTMLElement | null;
    setIsShowShare: (show: boolean) => void;
}

const Share: React.FC<ShareProps> = ({ targetElement, setIsShowShare }) => {
    const { t } = useLanguage()
    const {
        image,
        isImgLoading,
        error,
        takeScreenshot,
        download,
        reset
    } = useScreenshot({
        scale: 3, // 3倍高清
        type: 'image/jpeg',
        quality: 0.9,
        backgroundColor: null
    });

    const onClosePreview = () => {
        setIsShowShare(false)
        reset()
    }

    const onTakeScreenshot = () => {
        if (isImgLoading) return
        takeScreenshot(targetElement)
    }

    const previewNode = image && (
        <div className='previewMask'>
            <div className='previewContent'>
                <div className='previewImg'>
                    <img
                        src={image}
                        alt={t('share.previewAlt')}
                        style={{ width: '100%' }}
                    />
                </div>
                <div className='previewHandle'>
                    <button onClick={() => download(`screenshot_${Date.now()}.jpg`)}>
                        {t('share.save')}
                    </button>
                    <button onClick={onClosePreview}>
                        {t('share.close')}
                    </button>
                </div>
            </div>
        </div>
    )

    return (
        <>
            <div className='shareWrap'>
                <div className='shareItem savePicture' role="button" tabIndex={0}
                    aria-label={t('share.saveImage')} aria-disabled={isImgLoading}
                    onClick={onTakeScreenshot}
                    onKeyDown={event => {
                        if (event.key !== 'Enter' && event.key !== ' ') return;
                        event.preventDefault();
                        onTakeScreenshot();
                    }}>
                    <img src={PictuerIcon} alt="" />
                    <span>{isImgLoading ? t('share.generating') : t('share.saveImage')}</span>
                </div>
                {error && <span role="alert" className='shareError'>{error}</span>}
            </div>
            {previewNode && typeof document !== 'undefined' ? createPortal(previewNode, document.body) : null}
        </>

    )
}

export default Share
