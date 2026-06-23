import type { SVGProps } from "react";
export interface IconProps extends SVGProps<SVGSVGElement> {
    size?: number | string;
}
export declare function createIcon(path: React.ReactNode, displayName: string, defaultSize?: number): {
    ({ size, className, ...props }: IconProps): import("react").JSX.Element;
    displayName: string;
};
